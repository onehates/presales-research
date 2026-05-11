"""
NCES Common Core of Data Client — School district enrollment, school count, and federal funding data.

Data source: Urban Institute Education Data API (wraps NCES CCD)
  - Endpoint: https://educationdata.urban.org/api/v1/
  - No API key required, no auth, free public access
  - Rate limits: undocumented but generous; we add 0.5s delay between requests
  - Returns JSON with pagination

What it gives a Verkada SE:
  - School count within a district → drives multi_site_sprawl trigger
  - Enrollment numbers → scale indicator for sizing
  - Free/reduced lunch percentages → federal funding indicator → NDAA trigger
  - Title I status → federal funding anchor
  - Per-school addresses → geographic mapping for deployment planning

Fragility points:
  1. District name resolution is inexact. The API does not support name-based filtering —
     we must fetch all districts for a state (by FIPS code) and search locally. For Georgia
     (FIPS 13) this returns ~245 districts, which is manageable.
  2. Data is annual. Most recent available year varies (2022 is typical latest). We try
     2022, then fall back to 2021 if empty.
  3. School-level free/reduced lunch data may have NULL values for some schools.
  4. The API uses LEAID (7-digit NCES district ID) as the primary key. We must resolve
     district names to LEAIDs before fetching school-level data.
  5. Charter schools may appear as separate LEAs, not under the parent district.
  6. Enrollment numbers from CCD may differ from district-reported figures by 5-10%.

Extraction: Claude Haiku per CLAUDE.md model assignment (for trigger matching).
Cache TTL: 365 days (federal data is annual).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "sources"
PERSONA_PATH = PROJECT_ROOT / "persona" / "verkada-se.yml"

HAIKU_MODEL = "claude-haiku-4-5-20251001"

URBAN_API_BASE = "https://educationdata.urban.org/api/v1"

CACHE_TTL_DAYS = 365

REQUEST_INTERVAL = 0.5
_last_request_time = 0.0

# State name to FIPS code mapping (common states)
STATE_FIPS = {
    "AL": 1, "AK": 2, "AZ": 4, "AR": 5, "CA": 6, "CO": 8, "CT": 9, "DE": 10,
    "FL": 12, "GA": 13, "HI": 15, "ID": 16, "IL": 17, "IN": 18, "IA": 19,
    "KS": 20, "KY": 21, "LA": 22, "ME": 23, "MD": 24, "MA": 25, "MI": 26,
    "MN": 27, "MS": 28, "MO": 29, "MT": 30, "NE": 31, "NV": 32, "NH": 33,
    "NJ": 34, "NM": 35, "NY": 36, "NC": 37, "ND": 38, "OH": 39, "OK": 40,
    "OR": 41, "PA": 42, "RI": 44, "SC": 45, "SD": 46, "TN": 47, "TX": 48,
    "UT": 49, "VT": 50, "VA": 51, "WA": 53, "WV": 54, "WI": 55, "WY": 56,
    "DC": 11,
}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api_get(url: str, *, timeout: int = 30) -> dict:
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)

    resp = requests.get(url, timeout=timeout)
    _last_request_time = time.time()
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Persona loading
# ---------------------------------------------------------------------------

def _load_persona() -> dict:
    if not PERSONA_PATH.exists():
        return {}
    try:
        return yaml.safe_load(PERSONA_PATH.read_text()) or {}
    except Exception:
        return {}


def _load_persona_context(persona: dict) -> str:
    if not persona:
        return ""
    product = persona.get("product", {})
    lines = [f"Verkada product lines: {', '.join(product.get('lines', []))}"]
    lines.append(f"Positioning: {product.get('positioning', '')}")
    k12 = None
    for v in persona.get("icp", {}).get("verticals", []):
        if v.get("name") == "K-12":
            k12 = v
            break
    if k12:
        lines.append(f"K-12 key drivers: {', '.join(k12.get('key_drivers', []))}")
        lines.append(f"K-12 typical pain: {', '.join(k12.get('typical_pain', []))}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# District resolution
# ---------------------------------------------------------------------------

def resolve_district(district_name: str, state: str = "GA", year: int = 2022) -> dict | None:
    """
    Resolve a district name to NCES LEAID by scanning all districts in the state.
    Returns the best-matching district dict or None.
    """
    fips = STATE_FIPS.get(state.upper())
    if not fips:
        print(f"  [nces] unknown state: {state}", file=sys.stderr)
        return None

    url = f"{URBAN_API_BASE}/school-districts/ccd/directory/{year}/?fips={fips}"
    print(f"  [nces] fetching {state} districts for {year}...", file=sys.stderr)

    try:
        data = _api_get(url)
    except requests.HTTPError as e:
        print(f"  [nces] HTTP error: {e}", file=sys.stderr)
        return None

    districts = data.get("results", [])
    if not districts:
        # Try previous year
        if year > 2020:
            print(f"  [nces] no {year} data, trying {year - 1}...", file=sys.stderr)
            return resolve_district(district_name, state, year - 1)
        return None

    print(f"  [nces] {len(districts)} districts in {state}", file=sys.stderr)

    query = district_name.lower().strip()
    # Normalize common patterns
    query_norm = query.replace("public schools", "").replace("school district", "").replace("county schools", "county").strip()

    # Pass 1: exact match
    for d in districts:
        if d.get("lea_name", "").lower().strip() == query:
            return d

    # Pass 2: normalized match
    for d in districts:
        lea = d.get("lea_name", "").lower().strip()
        if query_norm and query_norm in lea:
            return d

    # Pass 3: token overlap
    query_tokens = set(query_norm.split())
    best = None
    best_score = 0
    for d in districts:
        lea = d.get("lea_name", "").lower().strip()
        lea_tokens = set(lea.split())
        overlap = len(query_tokens & lea_tokens)
        if overlap > best_score:
            best_score = overlap
            best = d

    if best and best_score >= 1:
        return best

    return None


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_schools(leaid: str, year: int = 2022) -> list[dict]:
    """Fetch all schools within a district."""
    url = f"{URBAN_API_BASE}/schools/ccd/directory/{year}/?leaid={leaid}"
    print(f"  [nces] fetching schools for LEAID {leaid}...", file=sys.stderr)

    all_schools = []
    page = 1
    while url:
        try:
            data = _api_get(url)
        except requests.HTTPError:
            break
        all_schools.extend(data.get("results", []))
        url = data.get("next")
        page += 1
        if page > 10:  # Safety limit
            break

    print(f"  [nces] {len(all_schools)} schools fetched", file=sys.stderr)
    return all_schools


def compute_funding_indicators(schools: list[dict], district: dict) -> dict:
    """Compute federal funding indicators from school-level data."""
    total_enrollment = 0
    total_frpl = 0
    total_free = 0
    title_i_count = 0
    schools_with_data = 0

    for s in schools:
        enrollment = s.get("enrollment")
        frpl = s.get("free_or_reduced_price_lunch")
        free = s.get("free_lunch")

        if enrollment and enrollment > 0:
            total_enrollment += enrollment
            schools_with_data += 1

        if frpl and frpl > 0:
            total_frpl += frpl

        if free and free > 0:
            total_free += free

        # lunch_program field: 1=yes, 2=CEP
        if s.get("lunch_program") in [1, 2]:
            title_i_count += 1

    frpl_pct = round(total_frpl / total_enrollment * 100, 1) if total_enrollment > 0 else None

    # District-level enrollment may differ from sum of schools
    district_enrollment = district.get("enrollment")

    return {
        "title_i_schools": title_i_count,
        "total_schools_with_data": schools_with_data,
        "district_enrollment": district_enrollment,
        "school_sum_enrollment": total_enrollment,
        "free_lunch_total": total_free,
        "frpl_total": total_frpl,
        "frpl_percentage": frpl_pct,
        "has_federal_funding": title_i_count > 0 or (frpl_pct is not None and frpl_pct > 40),
    }


# ---------------------------------------------------------------------------
# Haiku trigger matching
# ---------------------------------------------------------------------------

def analyze_with_haiku(district: dict, schools: list[dict], funding: dict,
                       entity_name: str, persona: dict) -> dict:
    """Haiku pass to match district data against verkada-se.yml triggers."""
    if not district:
        return {"status": "insufficient_data", "reason": "No district data to analyze"}

    client = anthropic.Anthropic()
    persona_ctx = _load_persona_context(persona)

    school_count = district.get("number_of_schools", len(schools))

    system_prompt = (
        "You are an education sector analyst reviewing school district data "
        "for a Verkada Solutions Engineer's pre-sales research tool.\n\n"
        "## Verkada Context\n"
        f"{persona_ctx}\n\n"
        "## Output Schema\n"
        "Output ONLY valid JSON:\n"
        '{"triggers_fired": [{"trigger_id": "string", "evidence": "string with specific numbers", '
        '"confidence": "high|medium|inference"}], '
        '"verkada_signals": [{"signal": "string", "evidence": "string", "confidence": "high|medium|inference"}], '
        '"district_summary": "2-3 sentences with specific numbers about this district"}\n\n'
        "## Trigger IDs to check\n"
        "- multi_site_sprawl: fires if school_count > 10\n"
        "- ndaa_compliance_pressure: fires if district receives federal funding (Title I, FRPL > 40%)\n"
        "- capital_project_signal: fires if evidence of new school construction\n"
        "## Anti-Genericness: cite specific numbers (enrollment, school count, FRPL %). "
        "Every sentence must reference this district specifically.\n"
    )

    user_msg = (
        f"District: {entity_name}\n"
        f"LEAID: {district.get('leaid')}\n"
        f"State: {district.get('state_location')}\n"
        f"Enrollment: {district.get('enrollment')}\n"
        f"Number of schools: {school_count}\n"
        f"County: {district.get('county_name')}\n"
        f"Funding indicators: {json.dumps(funding)}\n"
    )

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except (json.JSONDecodeError, anthropic.APIError, TypeError) as e:
        return {"status": "extraction_error", "reason": str(e)}


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def cache_path(company_slug: str) -> Path:
    return SOURCES_DIR / company_slug / "nces.json"


def read_cache(company_slug: str) -> dict | None:
    path = cache_path(company_slug)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    retrieved_at = data.get("retrieved_at")
    if not retrieved_at:
        return None

    retrieved_dt = datetime.fromisoformat(retrieved_at)
    age_days = (datetime.now(timezone.utc) - retrieved_dt).days
    if age_days > CACHE_TTL_DAYS:
        print(f"  [cache] nces.json is {age_days}d old (TTL={CACHE_TTL_DAYS}d), refetching", file=sys.stderr)
        return None

    print(f"  [cache] nces.json is {age_days}d old, within TTL", file=sys.stderr)
    return data


def write_cache(company_slug: str, data: dict) -> Path:
    path = cache_path(company_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))
    print(f"  [cache] wrote {path}", file=sys.stderr)
    return path


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def fetch_nces_data(district_name: str, *, state: str = "GA", force_refresh: bool = False) -> dict:
    """Full pipeline: resolve district → fetch schools → compute funding → Haiku → cache."""
    company_slug = slugify(district_name)

    if not force_refresh:
        cached = read_cache(company_slug)
        if cached is not None:
            return cached

    persona = _load_persona()

    # Step 1: Resolve district
    district = resolve_district(district_name, state)
    if not district:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "nces_ccd",
            "source_url": "https://nces.ed.gov/ccd/districtsearch/",
            "status": "insufficient_data",
            "reason": f"No NCES district found matching '{district_name}' in {state}. "
                      "Try the exact district name as it appears in NCES records.",
            "district": {"name": district_name, "state": state},
        }
        write_cache(company_slug, result)
        return result

    leaid = district["leaid"]
    year = district.get("year", 2022)
    print(f"  [nces] resolved: {district['lea_name']} (LEAID {leaid})", file=sys.stderr)

    # Step 2: Fetch schools
    schools = fetch_schools(leaid, year)

    # Step 3: Compute funding indicators
    funding = compute_funding_indicators(schools, district)

    # Step 4: Build per-school summary (top schools by enrollment)
    school_entries = []
    for s in sorted(schools, key=lambda x: x.get("enrollment") or 0, reverse=True):
        school_entries.append({
            "ncessch": s.get("ncessch"),
            "school_name": s.get("school_name"),
            "city": s.get("city_location"),
            "enrollment": s.get("enrollment"),
            "school_level": s.get("school_level"),
            "free_or_reduced_price_lunch": s.get("free_or_reduced_price_lunch"),
            "free_lunch": s.get("free_lunch"),
            "lunch_program": s.get("lunch_program"),
            "latitude": s.get("latitude"),
            "longitude": s.get("longitude"),
            "address": s.get("street_location"),
            "zip": s.get("zip_location"),
        })

    # Step 5: Haiku trigger analysis
    haiku_analysis = analyze_with_haiku(district, schools, funding, district_name, persona)

    # Step 6: Assemble and cache
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "nces_ccd",
        "source_url": f"https://nces.ed.gov/ccd/districtsearch/district_detail.asp?Search=1&details=1&ID2={leaid}",
        "data_year": year,
        "district_metadata": {
            "leaid": leaid,
            "lea_name": district.get("lea_name"),
            "state": district.get("state_location"),
            "county": district.get("county_name"),
            "city": district.get("city_location"),
            "phone": district.get("phone"),
            "agency_type": district.get("agency_type"),
            "urban_centric_locale": district.get("urban_centric_locale"),
            "latitude": district.get("latitude"),
            "longitude": district.get("longitude"),
        },
        "enrollment": {
            "total": district.get("enrollment"),
            "number_of_schools": district.get("number_of_schools"),
            "teachers_total_fte": district.get("teachers_total_fte"),
            "staff_total_fte": district.get("staff_total_fte"),
        },
        "funding_indicators": funding,
        "schools": school_entries,
        "haiku_analysis": haiku_analysis,
        "summary": {
            "district_name": district.get("lea_name"),
            "enrollment": district.get("enrollment"),
            "school_count": district.get("number_of_schools"),
            "frpl_percentage": funding.get("frpl_percentage"),
            "has_federal_funding": funding.get("has_federal_funding"),
            "source_quality": "primary",
            "confidence": "high",
        },
    }

    write_cache(company_slug, result)
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python nces.py <district_name> [--state XX] [--force]", file=sys.stderr)
        print("  e.g.: python nces.py 'Atlanta Public Schools'", file=sys.stderr)
        print("        python nces.py 'Fulton County Schools' --state GA", file=sys.stderr)
        print("", file=sys.stderr)
        print("  No API key required. Data from NCES Common Core of Data via Urban Institute API.", file=sys.stderr)
        sys.exit(1)

    district_name = sys.argv[1]
    force = "--force" in sys.argv

    state = "GA"
    if "--state" in sys.argv:
        idx = sys.argv.index("--state")
        if idx + 1 < len(sys.argv):
            state = sys.argv[idx + 1]

    try:
        result = fetch_nces_data(district_name, state=state, force_refresh=force)

        if result.get("status") == "insufficient_data":
            print(f"\n  {result['reason']}", file=sys.stderr)
        else:
            summary = result["summary"]
            print(
                f"\n  Done: {summary['district_name']}\n"
                f"  Enrollment: {summary['enrollment']}\n"
                f"  Schools: {summary['school_count']}\n"
                f"  FRPL %: {summary['frpl_percentage']}\n"
                f"  Federal funding: {summary['has_federal_funding']}\n"
                f"  Cached to: sources/{slugify(district_name)}/nces.json",
                file=sys.stderr,
            )

        print(json.dumps(result, indent=2, default=str))

    except requests.HTTPError as e:
        print(f"ERROR: NCES API error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
