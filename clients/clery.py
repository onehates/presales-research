"""
Clery Act Campus Crime Statistics Client — Fetches campus safety data from OPE.

Data source: U.S. Department of Education Campus Safety and Security tool
  - Undocumented API: https://ope.ed.gov/campussafety/api/institution/{unitid}
  - No API key required, no auth
  - Returns 3 years of crime statistics by category
  - Institution search: POST to /api/institution/search (but returns all results unfiltered)
  - We resolve names via IPEDS directory (Urban Institute API) instead

What it gives a Verkada SE:
  - Crime trends (rising violent crime → incident_recent_12mo trigger)
  - Campus scale (enrollment, multi-campus → multi_site_sprawl trigger)
  - Specific crime categories that map to Verkada products:
    - Burglary/theft → cameras, access control
    - Weapons violations → weapon detection
    - Violence → real-time alerting, cameras
    - Drug/liquor → environmental monitoring
  - Year-over-year trend analysis for discovery conversation ammunition

Fragility points:
  1. The OPE API is UNDOCUMENTED. It backs an Angular SPA at ope.ed.gov/campussafety.
     Endpoint structure was reverse-engineered from the app.min.js bundle.
     The API may change without notice.
  2. Institution ID (UNITID) resolution requires a separate IPEDS lookup.
     We use the Urban Institute API for this, which is more reliable than the
     OPE search endpoint (which returns all institutions alphabetically).
  3. Crime data is HTML-embedded in the response JSON. Category names include
     Angular template directives that must be stripped for clean extraction.
  4. The API returns data for the institution-level aggregate. Campus-level
     breakdowns (on-campus, student housing, public property) are in separate
     "screens" within each group, identified by Screen RSN.
  5. Survey year lags: 2024 data (most recent) covers incidents from
     calendar years 2022-2024. There is a ~1 year reporting lag.
  6. Small institutions may report all zeros, which is valid — it means
     no Clery-reportable incidents, not missing data.

Screen RSN mapping (reverse-engineered):
  Criminal Offenses:
    10100 = On campus total
    10101 = On-campus student housing
    10102 = Noncampus
    10103 = Public property
  VAWA Offenses:
    10131 = On campus
    10132 = Student housing
    10133 = Noncampus
    10134 = Public property
  Arrests:
    120 = On campus
    121 = Student housing
    125 = Noncampus
    126 = Public property
  Disciplinary Actions:
    122 = On campus
    129 = Student housing
    127 = Noncampus
    128 = Public property

Extraction: Claude Haiku per CLAUDE.md model assignment.
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

OPE_API_BASE = "https://ope.ed.gov/campussafety/api"
IPEDS_API_BASE = "https://educationdata.urban.org/api/v1/college-university/ipeds/directory"

CACHE_TTL_DAYS = 365

REQUEST_INTERVAL = 0.5
_last_request_time = 0.0

# Screen RSNs for on-campus totals (most relevant for analysis)
SCREEN_MAP = {
    "criminal_offenses_oncampus": 10100,
    "criminal_offenses_housing": 10101,
    "criminal_offenses_noncampus": 10102,
    "criminal_offenses_public": 10103,
    "vawa_oncampus": 10131,
    "vawa_housing": 10132,
    "arrests_oncampus": 120,
    "arrests_housing": 121,
    "disciplinary_oncampus": 122,
    "disciplinary_housing": 129,
}

# Crime category labels (extracted from Angular templates in the API response)
CRIMINAL_OFFENSE_LABELS = [
    "murder_non_negligent_manslaughter",
    "negligent_manslaughter",
    "rape",
    "fondling",
    "incest",
    "statutory_rape",
    "robbery",
    "aggravated_assault",
    "burglary",
    "motor_vehicle_theft",
    "arson",
]

VAWA_LABELS = [
    "domestic_violence",
    "dating_violence",
    "stalking",
]

ARREST_LABELS = [
    "weapons",
    "drug_abuse",
    "liquor_law",
]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api_get(url: str, *, timeout: int = 30) -> requests.Response:
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)

    resp = requests.get(url, timeout=timeout)
    _last_request_time = time.time()
    return resp


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
    higher_ed = None
    for v in persona.get("icp", {}).get("verticals", []):
        if v.get("name") == "HigherEd":
            higher_ed = v
            break
    if higher_ed:
        lines.append(f"HigherEd key drivers: {', '.join(higher_ed.get('key_drivers', []))}")
        lines.append(f"HigherEd typical pain: {', '.join(higher_ed.get('typical_pain', []))}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Institution resolution via IPEDS
# ---------------------------------------------------------------------------

def resolve_institution(name: str, state: str = "GA") -> dict | None:
    """Resolve university name to UNITID via IPEDS directory."""
    # Map state to FIPS
    fips_map = {
        "AL": 1, "AK": 2, "AZ": 4, "AR": 5, "CA": 6, "CO": 8, "CT": 9, "DE": 10,
        "FL": 12, "GA": 13, "HI": 15, "ID": 16, "IL": 17, "IN": 18, "IA": 19,
        "KS": 20, "KY": 21, "LA": 22, "ME": 23, "MD": 24, "MA": 25, "MI": 26,
        "MN": 27, "MS": 28, "MO": 29, "MT": 30, "NE": 31, "NV": 32, "NH": 33,
        "NJ": 34, "NM": 35, "NY": 36, "NC": 37, "ND": 38, "OH": 39, "OK": 40,
        "OR": 41, "PA": 42, "RI": 44, "SC": 45, "SD": 46, "TN": 47, "TX": 48,
        "UT": 49, "VT": 50, "VA": 51, "WA": 53, "WV": 54, "WI": 55, "WY": 56,
        "DC": 11,
    }
    fips = fips_map.get(state.upper())
    if not fips:
        return None

    url = f"{IPEDS_API_BASE}/2022/?fips={fips}"
    print(f"  [ipeds] fetching {state} institutions...", file=sys.stderr)

    try:
        resp = _api_get(url)
        resp.raise_for_status()
        data = resp.json()
    except (requests.HTTPError, ValueError) as e:
        print(f"  [ipeds] error: {e}", file=sys.stderr)
        return None

    results = data.get("results", [])
    query = name.lower().strip()

    # Pass 1: exact match
    for r in results:
        if r.get("inst_name", "").lower().strip() == query:
            return r

    # Pass 2: contains match
    for r in results:
        inst = r.get("inst_name", "").lower()
        if query in inst or all(w in inst for w in query.split()):
            return r

    # Pass 3: key words match
    query_words = set(query.split())
    best = None
    best_score = 0
    for r in results:
        inst_words = set(r.get("inst_name", "").lower().split())
        score = len(query_words & inst_words)
        if score > best_score:
            best_score = score
            best = r

    if best and best_score >= 2:
        return best

    return None


# ---------------------------------------------------------------------------
# OPE Crime Data Fetching and Parsing
# ---------------------------------------------------------------------------

def fetch_crime_data(unitid: int) -> dict | None:
    """Fetch crime data from OPE campus safety API."""
    url = f"{OPE_API_BASE}/institution/{unitid}"
    print(f"  [ope] fetching crime data for UNITID {unitid}...", file=sys.stderr)

    resp = _api_get(url)
    if resp.status_code != 200:
        print(f"  [ope] HTTP {resp.status_code}", file=sys.stderr)
        return None

    try:
        return resp.json()
    except ValueError:
        return None


def _strip_html(text: str) -> str:
    """Strip Angular template directives and HTML from cell text."""
    # Remove everything inside <a> tags but keep the text between them
    text = re.sub(r'<a[^>]*>', '', text)
    text = re.sub(r'</a>', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.strip()
    # Clean up common prefixes
    text = re.sub(r'^[a-z]\.\s*', '', text)
    return text


def parse_screen(screen: dict) -> dict:
    """Parse a single screen (table) of crime data into structured format."""
    rows = screen.get("Rows", [])
    if not rows:
        return {}

    # Extract years from header row (typically row index 4)
    years = []
    crimes = {}

    for row in rows:
        cells = row.get("Cells", [])
        if not cells:
            continue

        cell_texts = []
        for c in cells:
            html = c.get("Html", "")
            data = c.get("Data", {})
            if isinstance(data, dict) and data:
                cell_texts.append(str(next(iter(data.values()), html)))
            else:
                cell_texts.append(html)

        # Header row — contains years
        if row.get("RowIndex") == 4:
            for t in cell_texts[1:]:
                t = t.strip()
                if t.isdigit() and len(t) == 4:
                    years.append(int(t))
            continue

        # Data rows — first cell is crime type, rest are counts
        if len(cell_texts) >= 2 and years:
            crime_name = _strip_html(cell_texts[0])
            if not crime_name or crime_name in ["Total", ""]:
                continue

            values = {}
            for i, year in enumerate(years):
                idx = i + 1
                if idx < len(cell_texts):
                    try:
                        values[year] = int(cell_texts[idx])
                    except (ValueError, TypeError):
                        values[year] = None

            if crime_name and values:
                # Create a stable key
                key = re.sub(r'[^a-z0-9]+', '_', crime_name.lower()).strip('_')
                crimes[key] = {
                    "label": crime_name,
                    "by_year": values,
                }

    return {"years": years, "crimes": crimes}


def parse_crime_response(data: dict) -> dict:
    """Parse the full OPE API response into structured crime statistics."""
    header = data.get("Header", {})
    institution = header.get("Institution", {})
    campuses = header.get("Campuses", [])

    parsed = {
        "institution": {
            "unitid": institution.get("ID"),
            "name": institution.get("Name"),
            "opeid": (institution.get("OpeID") or "").strip(),
            "city": institution.get("City"),
            "state": institution.get("StateCode"),
            "enrollment": institution.get("Enrollment"),
            "survey_year": institution.get("SurveyYear"),
            "is_single_campus": institution.get("IsSingleCampus"),
            "website": institution.get("Website"),
        },
        "campuses": [
            {
                "unitid": c.get("UnitID"),
                "name": c.get("Name"),
                "city": c.get("City"),
                "state": c.get("State"),
            }
            for c in campuses
        ],
        "crime_stats": {},
    }

    # Parse each group
    group_map = {}
    for group in data.get("Groups", []):
        group_id = group.get("ID")
        group_desc = group.get("Description", "")
        group_map[group_id] = group_desc

        for screen in group.get("Screens", []):
            rsn = screen.get("Rsn")

            # Find the location label from SCREEN_MAP
            location = None
            for loc_key, loc_rsn in SCREEN_MAP.items():
                if loc_rsn == rsn:
                    location = loc_key
                    break

            if not location:
                continue

            screen_data = parse_screen(screen)
            if screen_data.get("crimes"):
                parsed["crime_stats"][location] = screen_data

    return parsed


def compute_trends(parsed: dict) -> dict:
    """Compute year-over-year trends for key crime categories."""
    trends = {}

    # Focus on on-campus totals
    oncampus = parsed.get("crime_stats", {}).get("criminal_offenses_oncampus", {})
    vawa = parsed.get("crime_stats", {}).get("vawa_oncampus", {})
    arrests = parsed.get("crime_stats", {}).get("arrests_oncampus", {})

    all_stats = {}
    for section_name, section in [("criminal", oncampus), ("vawa", vawa), ("arrests", arrests)]:
        for key, crime in section.get("crimes", {}).items():
            by_year = crime.get("by_year", {})
            years_sorted = sorted(by_year.keys())
            if len(years_sorted) >= 2:
                latest = by_year[years_sorted[-1]]
                previous = by_year[years_sorted[-2]]
                if latest is not None and previous is not None:
                    change = latest - previous
                    pct_change = round(change / previous * 100, 1) if previous > 0 else None
                    all_stats[f"{section_name}_{key}"] = {
                        "label": crime["label"],
                        "latest_year": years_sorted[-1],
                        "latest_value": latest,
                        "previous_value": previous,
                        "change": change,
                        "pct_change": pct_change,
                        "direction": "rising" if change > 0 else "falling" if change < 0 else "flat",
                    }

    # Flag significant increases (potential triggers)
    significant = {k: v for k, v in all_stats.items()
                   if v["change"] > 0 and v["latest_value"] >= 3}

    return {
        "all_trends": all_stats,
        "significant_increases": significant,
        "violent_crime_trend": _compute_violent_trend(oncampus, vawa),
    }


def _compute_violent_trend(oncampus: dict, vawa: dict) -> dict:
    """Compute aggregate violent crime trend."""
    violent_keys = ["murder_non_negligent_manslaughter", "rape", "fondling",
                    "robbery", "aggravated_assault"]

    years_totals = {}
    for section in [oncampus, vawa]:
        for key, crime in section.get("crimes", {}).items():
            if any(vk in key for vk in violent_keys + ["domestic_violence", "dating_violence", "stalking"]):
                for year, val in crime.get("by_year", {}).items():
                    if val is not None:
                        years_totals[year] = years_totals.get(year, 0) + val

    years_sorted = sorted(years_totals.keys())
    if len(years_sorted) >= 2:
        latest = years_totals[years_sorted[-1]]
        previous = years_totals[years_sorted[-2]]
        return {
            "latest_year": years_sorted[-1],
            "latest_total": latest,
            "previous_total": previous,
            "change": latest - previous,
            "direction": "rising" if latest > previous else "falling" if latest < previous else "flat",
        }
    return {}


# ---------------------------------------------------------------------------
# Haiku trigger matching
# ---------------------------------------------------------------------------

def analyze_with_haiku(parsed: dict, trends: dict, institution_name: str, persona: dict) -> dict:
    """Haiku pass to match crime data against verkada-se.yml triggers."""
    inst = parsed.get("institution", {})
    if not inst.get("unitid"):
        return {"status": "insufficient_data", "reason": "No institution data"}

    client = anthropic.Anthropic()
    persona_ctx = _load_persona_context(persona)

    system_prompt = (
        "You are a campus safety analyst reviewing Clery Act crime statistics "
        "for a Verkada Solutions Engineer's pre-sales research tool.\n\n"
        "## Verkada Context\n"
        f"{persona_ctx}\n\n"
        "## Output Schema\n"
        "Output ONLY valid JSON:\n"
        '{"triggers_fired": [{"trigger_id": "string", "evidence": "string with specific numbers and years", '
        '"confidence": "high|medium|inference"}], '
        '"verkada_signals": [{"signal": "string", "category": "string", '
        '"evidence": "string", "confidence": "high|medium|inference"}], '
        '"campus_safety_summary": "2-3 sentences summarizing key findings with specific numbers"}\n\n'
        "## Trigger IDs to check\n"
        "- incident_recent_12mo: fires if violent crime is rising or any category shows significant increase\n"
        "- multi_site_sprawl: fires if institution has multiple campuses\n"
        "- ndaa_compliance_pressure: fires if institution is public (receives federal funding)\n"
        "## Anti-Genericness: cite specific crime counts and year-over-year changes. "
        "Every claim must reference this institution by name with numbers.\n"
    )

    # Build compact crime summary
    crime_summary = {}
    for loc, data in parsed.get("crime_stats", {}).items():
        if "oncampus" in loc:
            crimes = {}
            for key, crime in data.get("crimes", {}).items():
                crimes[crime["label"]] = crime["by_year"]
            crime_summary[loc] = crimes

    user_msg = (
        f"Institution: {institution_name}\n"
        f"UNITID: {inst.get('unitid')}\n"
        f"State: {inst.get('state')}\n"
        f"Enrollment: {inst.get('enrollment')}\n"
        f"Campuses: {len(parsed.get('campuses', []))}\n"
        f"Is public: {inst.get('opeid', '').startswith('0')}\n\n"
        f"On-campus crime stats:\n{json.dumps(crime_summary, indent=1)}\n\n"
        f"Trends:\n{json.dumps(trends, indent=1)}"
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
    return SOURCES_DIR / company_slug / "clery.json"


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
        print(f"  [cache] clery.json is {age_days}d old (TTL={CACHE_TTL_DAYS}d), refetching", file=sys.stderr)
        return None

    print(f"  [cache] clery.json is {age_days}d old, within TTL", file=sys.stderr)
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


def fetch_clery_data(institution_name: str, *, state: str = "GA",
                     unitid: int = 0, force_refresh: bool = False) -> dict:
    """Full pipeline: resolve institution → fetch OPE data → parse → trends → Haiku → cache."""
    company_slug = slugify(institution_name)

    if not force_refresh:
        cached = read_cache(company_slug)
        if cached is not None:
            return cached

    persona = _load_persona()

    # Step 1: Resolve institution
    if not unitid:
        inst_info = resolve_institution(institution_name, state)
        if not inst_info:
            result = {
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "source": "ope_campus_safety",
                "source_url": f"{OPE_API_BASE}/institution/",
                "status": "insufficient_data",
                "reason": f"No institution found matching '{institution_name}' in {state} via IPEDS.",
                "institution": {"name": institution_name, "state": state},
            }
            write_cache(company_slug, result)
            return result
        unitid = inst_info.get("unitid")
        print(f"  [ipeds] resolved: {inst_info.get('inst_name')} (UNITID {unitid})", file=sys.stderr)

    # Step 2: Fetch OPE crime data
    raw_data = fetch_crime_data(unitid)
    if not raw_data or not raw_data.get("Groups"):
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "ope_campus_safety",
            "source_url": f"{OPE_API_BASE}/institution/{unitid}",
            "status": "insufficient_data",
            "reason": f"OPE API returned no crime data for UNITID {unitid}.",
            "institution": {"name": institution_name, "unitid": unitid, "state": state},
        }
        write_cache(company_slug, result)
        return result

    # Step 3: Parse
    parsed = parse_crime_response(raw_data)
    print(f"  [ope] parsed {len(parsed['crime_stats'])} location categories", file=sys.stderr)

    # Step 4: Compute trends
    trends = compute_trends(parsed)
    sig_count = len(trends.get("significant_increases", {}))
    print(f"  [trends] {sig_count} significant increase(s)", file=sys.stderr)

    # Step 5: Haiku analysis
    haiku_analysis = analyze_with_haiku(parsed, trends, institution_name, persona)

    # Step 6: Assemble and cache
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "ope_campus_safety",
        "source_url": f"https://ope.ed.gov/campussafety/#/institution/details/{unitid}",
        "institution_metadata": parsed["institution"],
        "campuses": parsed["campuses"],
        "crime_stats_by_location": parsed["crime_stats"],
        "trend_analysis": trends,
        "haiku_analysis": haiku_analysis,
        "summary": {
            "institution_name": parsed["institution"].get("name"),
            "enrollment": parsed["institution"].get("enrollment"),
            "campus_count": len(parsed["campuses"]),
            "survey_year": parsed["institution"].get("survey_year"),
            "significant_increases": sig_count,
            "violent_crime_direction": trends.get("violent_crime_trend", {}).get("direction"),
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
        print("Usage: python clery.py <institution_name> [--state XX] [--unitid NNNNNN] [--force]", file=sys.stderr)
        print("  e.g.: python clery.py 'Georgia Institute of Technology'", file=sys.stderr)
        print("        python clery.py 'Georgia State University' --state GA", file=sys.stderr)
        print("        python clery.py 'MIT' --unitid 166683 --state MA", file=sys.stderr)
        print("", file=sys.stderr)
        print("  No API key required. Data from DOE Campus Safety & Security database.", file=sys.stderr)
        sys.exit(1)

    institution_name = sys.argv[1]
    force = "--force" in sys.argv

    state = "GA"
    if "--state" in sys.argv:
        idx = sys.argv.index("--state")
        if idx + 1 < len(sys.argv):
            state = sys.argv[idx + 1]

    unitid = 0
    if "--unitid" in sys.argv:
        idx = sys.argv.index("--unitid")
        if idx + 1 < len(sys.argv):
            unitid = int(sys.argv[idx + 1])

    try:
        result = fetch_clery_data(institution_name, state=state, unitid=unitid, force_refresh=force)

        if result.get("status") == "insufficient_data":
            print(f"\n  {result['reason']}", file=sys.stderr)
        else:
            summary = result["summary"]
            print(
                f"\n  Done: {summary['institution_name']}\n"
                f"  Enrollment: {summary['enrollment']}\n"
                f"  Campuses: {summary['campus_count']}\n"
                f"  Survey year: {summary['survey_year']}\n"
                f"  Significant increases: {summary['significant_increases']}\n"
                f"  Violent crime trend: {summary['violent_crime_direction']}\n"
                f"  Cached to: sources/{slugify(institution_name)}/clery.json",
                file=sys.stderr,
            )

        print(json.dumps(result, indent=2, default=str))

    except requests.HTTPError as e:
        print(f"ERROR: OPE API error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
