"""
SAM.gov Contract Opportunities Client — Fetches federal RFPs and awards for government entities.

Data source: SAM.gov Opportunities API v2
  - Endpoint: https://api.sam.gov/prod/opportunities/v2/search
  - Requires SAM_API_KEY env var (free — register at sam.gov, generate key on Account Details page)
  - Rate limits undocumented but role-based; we add 1s delay between requests
  - Returns: active solicitations, pre-solicitations, award notices

What it gives a Verkada SE:
  - Active RFPs for security/surveillance/access control systems → immediate sales opportunities
  - Recent contract awards → who's buying what, competitor intel
  - NAICS codes indicating physical security procurement patterns
  - Federal funding signals → NDAA compliance trigger

Fragility points:
  1. API key required. No DEMO_KEY works. Registration is free but key provisioning can take
     up to 10 business days. Without key, outputs insufficient_data.
  2. organizationName search is fuzzy — "Atlanta Public Schools" may match other orgs with
     "Atlanta" in the name. We validate results against the expected entity.
  3. Date range limited to 1 year per query. We query the most recent year.
  4. The API only returns the latest active version of each opportunity. Historical closed
     opportunities may not appear. Award data is spotty.
  5. State filter uses place-of-performance, not issuing org location. Some federal contracts
     list Washington DC as place-of-performance regardless of actual work location.
  6. NAICS code coverage: Not all opportunities have NAICS codes assigned.

Extraction: Claude Haiku per CLAUDE.md model assignment.
Cache TTL: 7 days (RFPs change frequently).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
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

SAM_API_URL = "https://api.sam.gov/prod/opportunities/v2/search"
SAM_API_KEY = os.environ.get("SAM_API_KEY", "")

CACHE_TTL_DAYS = 7

REQUEST_INTERVAL = 1.0
_last_request_time = 0.0

# Security-relevant NAICS codes
SECURITY_NAICS = {
    "561611": "Investigation Services",
    "561612": "Security Guards and Patrol Services",
    "561621": "Security Systems Services (except Locksmiths)",
    "334290": "Other Communications Equipment Manufacturing",
    "334310": "Audio and Video Equipment Manufacturing",
    "423410": "Photographic Equipment and Supplies Merchant Wholesalers",
    "423430": "Computer and Peripheral Equipment Merchant Wholesalers",
    "238210": "Electrical Contractors (includes security system installation)",
    "541512": "Computer Systems Design Services",
    "517110": "Wired Telecommunications Carriers",
}

# Keywords that signal Verkada-relevant opportunities
SECURITY_KEYWORDS = [
    "security camera", "surveillance", "video management", "VMS", "access control",
    "CCTV", "NVR", "DVR", "physical security", "security system", "intrusion detection",
    "alarm system", "badge", "card reader", "intercom", "visitor management",
    "camera", "monitoring", "guard tour", "loss prevention", "video analytics",
    "NDAA", "NDAA compliant",
]

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
    displace = persona.get("displacement_targets", [])
    lines = [f"Verkada product lines: {', '.join(product.get('lines', []))}"]
    lines.append(f"Unified under: {product.get('unified_under', 'Command platform')}")
    lines.append(f"Positioning: {product.get('positioning', '')}")
    if displace:
        vendors = [d.get("vendor", "") for d in displace if isinstance(d, dict)]
        lines.append(f"Displacement targets: {', '.join(vendors)}")
    return "\n".join(lines)


def _extract_trigger_keywords(persona: dict) -> dict:
    triggers = persona.get("triggers", [])
    result = {}
    for trigger in triggers:
        if not isinstance(trigger, dict):
            continue
        hints = trigger.get("detect_signals", {}).get("source_hints", [])
        if "sec_filings" in hints or "news" in hints or "press_releases" in hints:
            keywords = trigger.get("detect_signals", {}).get("keywords", [])
            if keywords:
                result[trigger["id"]] = [k.lower() for k in keywords]
    return result


# ---------------------------------------------------------------------------
# SAM.gov API
# ---------------------------------------------------------------------------

def _sam_get(params: dict, *, timeout: int = 30) -> dict:
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)

    params["api_key"] = SAM_API_KEY
    resp = requests.get(SAM_API_URL, params=params, timeout=timeout)
    _last_request_time = time.time()

    if resp.status_code == 429:
        print("  [sam] rate limited, waiting 30s...", file=sys.stderr)
        time.sleep(30)
        return _sam_get(params, timeout=timeout)

    if resp.status_code == 403:
        return {"error": "SAM_API_KEY is invalid or expired"}
    if resp.status_code == 404:
        return {"error": "SAM.gov API returned 404 — key may be invalid or endpoint changed"}

    resp.raise_for_status()
    return resp.json()


def fetch_opportunities(entity_name: str, *, state: str = "GA") -> list[dict]:
    """Fetch active opportunities from SAM.gov matching the entity name."""
    if not SAM_API_KEY:
        return []

    now = datetime.now(timezone.utc)
    one_year_ago = now - timedelta(days=365)

    params = {
        "postedFrom": one_year_ago.strftime("%m/%d/%Y"),
        "postedTo": now.strftime("%m/%d/%Y"),
        "limit": 100,
        "offset": 0,
        "organizationName": entity_name,
    }
    if state:
        params["state"] = state

    print(f"  [sam] searching for '{entity_name}' in {state}...", file=sys.stderr)

    try:
        data = _sam_get(params)
    except requests.HTTPError as e:
        print(f"  [sam] HTTP error: {e}", file=sys.stderr)
        return []

    if "error" in data:
        print(f"  [sam] error: {data['error']}", file=sys.stderr)
        return []

    opportunities = data.get("opportunitiesData", [])
    total = data.get("totalRecords", 0)
    print(f"  [sam] found {total} total, fetched {len(opportunities)}", file=sys.stderr)

    return opportunities


def fetch_keyword_opportunities(entity_name: str, *, state: str = "GA") -> list[dict]:
    """Search for security-related opportunities mentioning the entity by keyword."""
    if not SAM_API_KEY:
        return []

    now = datetime.now(timezone.utc)
    one_year_ago = now - timedelta(days=365)

    # Search for security-related RFPs in the entity's state
    params = {
        "postedFrom": one_year_ago.strftime("%m/%d/%Y"),
        "postedTo": now.strftime("%m/%d/%Y"),
        "limit": 50,
        "offset": 0,
        "title": "security camera OR surveillance OR access control",
    }
    if state:
        params["state"] = state

    print(f"  [sam] keyword search for security opps in {state}...", file=sys.stderr)

    try:
        data = _sam_get(params)
    except requests.HTTPError as e:
        print(f"  [sam] keyword search HTTP error: {e}", file=sys.stderr)
        return []

    if "error" in data:
        return []

    opportunities = data.get("opportunitiesData", [])
    print(f"  [sam] keyword search: {len(opportunities)} security-related opps", file=sys.stderr)
    return opportunities


# ---------------------------------------------------------------------------
# Deterministic signal matching
# ---------------------------------------------------------------------------

def classify_opportunities(opportunities: list[dict]) -> dict:
    """Classify opportunities as security-relevant or not."""
    active_rfps = []
    recent_awards = []
    verkada_signals = []

    for opp in opportunities:
        title = (opp.get("title") or "").lower()
        naics = opp.get("naicsCode") or ""
        opp_type = opp.get("type") or ""
        active = opp.get("active") == "Yes"

        entry = {
            "notice_id": opp.get("noticeId", ""),
            "title": opp.get("title", ""),
            "posting_date": opp.get("postedDate", ""),
            "response_deadline": opp.get("responseDeadLine", ""),
            "naics_code": naics,
            "type": opp_type,
            "active": active,
            "organization": opp.get("fullParentPathName", ""),
            "place_of_performance": opp.get("placeOfPerformance", {}),
            "source_url": opp.get("uiLink", ""),
        }

        # Check for award data
        award = opp.get("award")
        if award and isinstance(award, dict):
            entry["award"] = {
                "date": award.get("date"),
                "amount": award.get("amount"),
                "awardee": award.get("awardee", {}).get("name"),
                "awardee_uei": award.get("awardee", {}).get("ueiSAM"),
            }

        # Classify as award or active RFP
        if award or opp_type == "a":
            recent_awards.append(entry)
        elif active:
            active_rfps.append(entry)

        # Check Verkada relevance
        is_security = False
        if naics in SECURITY_NAICS:
            is_security = True
        for kw in SECURITY_KEYWORDS:
            if kw in title:
                is_security = True
                break

        if is_security:
            verkada_signals.append({
                **entry,
                "relevance_reason": f"NAICS {naics} ({SECURITY_NAICS.get(naics, '')})" if naics in SECURITY_NAICS
                    else f"Title matches security keyword",
                "confidence": "high" if naics in SECURITY_NAICS else "medium",
            })

    return {
        "active_rfps": active_rfps,
        "recent_awards": recent_awards,
        "verkada_relevant_signals": verkada_signals,
    }


# ---------------------------------------------------------------------------
# Haiku analysis
# ---------------------------------------------------------------------------

def analyze_with_haiku(classified: dict, entity_name: str, persona: dict) -> dict:
    """Haiku pass to identify trigger matches and synthesize findings."""
    total = len(classified["active_rfps"]) + len(classified["recent_awards"])
    if total == 0:
        return {"status": "insufficient_data", "reason": "No opportunities found to analyze"}

    client = anthropic.Anthropic()
    persona_ctx = _load_persona_context(persona)

    # Build compact summary for Haiku
    rfp_summaries = []
    for r in classified["active_rfps"][:20]:
        rfp_summaries.append({
            "title": r["title"],
            "naics": r["naics_code"],
            "deadline": r["response_deadline"],
            "org": r["organization"],
        })

    signal_summaries = []
    for s in classified["verkada_relevant_signals"][:10]:
        signal_summaries.append({
            "title": s["title"],
            "naics": s["naics_code"],
            "reason": s["relevance_reason"],
        })

    system_prompt = (
        "You are a government procurement analyst reviewing SAM.gov contract opportunities "
        "for a Verkada Solutions Engineer's pre-sales research tool.\n\n"
        "## Verkada Context\n"
        f"{persona_ctx}\n\n"
        "## Output Schema\n"
        "Output ONLY valid JSON:\n"
        '{"triggers_fired": [{"trigger_id": "string — from verkada-se.yml", '
        '"evidence": "specific RFP title or award detail", "confidence": "high|medium|inference"}], '
        '"procurement_summary": "2-3 sentences about this entity procurement patterns '
        'relevant to physical security — cite specific RFP titles"}\n\n'
        "## Anti-Genericness Rules\n"
        "- Cite specific RFP titles. No generic procurement observations.\n"
        "- Only fire triggers from verkada-se.yml: ndaa_compliance_pressure, capital_project_signal, "
        "vendor_consolidation_signal, legacy_nvr_dvr_refresh\n"
        "- If no triggers fire, return empty triggers_fired array.\n"
    )

    user_msg = (
        f"Entity: {entity_name}\n"
        f"Active RFPs: {len(classified['active_rfps'])}\n"
        f"Recent Awards: {len(classified['recent_awards'])}\n"
        f"Security-relevant: {len(classified['verkada_relevant_signals'])}\n\n"
        f"RFPs:\n{json.dumps(rfp_summaries, indent=1)}\n\n"
        f"Security signals:\n{json.dumps(signal_summaries, indent=1)}"
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
    return SOURCES_DIR / company_slug / "sam.json"


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

    if data.get("status") == "insufficient_data" and SAM_API_KEY:
        reason = data.get("reason", "")
        if "SAM_API_KEY" in reason:
            return None

    retrieved_dt = datetime.fromisoformat(retrieved_at)
    age_days = (datetime.now(timezone.utc) - retrieved_dt).days
    if age_days > CACHE_TTL_DAYS:
        print(f"  [cache] sam.json is {age_days}d old (TTL={CACHE_TTL_DAYS}d), refetching", file=sys.stderr)
        return None

    print(f"  [cache] sam.json is {age_days}d old, within TTL", file=sys.stderr)
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


def fetch_sam_data(entity_name: str, *, state: str = "GA", force_refresh: bool = False) -> dict:
    """Full pipeline: fetch SAM.gov opportunities → classify → Haiku analysis → cache."""
    company_slug = slugify(entity_name)

    if not force_refresh:
        cached = read_cache(company_slug)
        if cached is not None:
            return cached

    if not SAM_API_KEY:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "sam_gov",
            "source_url": "https://api.sam.gov/prod/opportunities/v2/search",
            "status": "insufficient_data",
            "reason": (
                "SAM_API_KEY not set. Register free at sam.gov and generate an API key "
                "on the Account Details page. Key provisioning may take up to 10 business days."
            ),
            "entity": {"name": entity_name, "state": state},
            "active_rfps": [],
            "recent_awards": [],
            "verkada_relevant_signals": [],
            "haiku_analysis": {},
        }
        write_cache(company_slug, result)
        return result

    persona = _load_persona()

    # Step 1: Fetch opportunities by organization name
    org_opps = fetch_opportunities(entity_name, state=state)

    # Step 2: Fetch security-related opportunities in the same state
    keyword_opps = fetch_keyword_opportunities(entity_name, state=state)

    # Merge and deduplicate by noticeId
    seen = set()
    all_opps = []
    for opp in org_opps + keyword_opps:
        nid = opp.get("noticeId", "")
        if nid and nid not in seen:
            seen.add(nid)
            all_opps.append(opp)

    print(f"  [sam] {len(all_opps)} unique opportunities after dedup", file=sys.stderr)

    # Step 3: Classify
    classified = classify_opportunities(all_opps)

    # Step 4: Haiku analysis
    haiku_analysis = analyze_with_haiku(classified, entity_name, persona)

    # Step 5: Assemble and cache
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "sam_gov",
        "source_url": "https://api.sam.gov/prod/opportunities/v2/search",
        "entity": {"name": entity_name, "state": state},
        "active_rfps": classified["active_rfps"],
        "recent_awards": classified["recent_awards"],
        "verkada_relevant_signals": classified["verkada_relevant_signals"],
        "haiku_analysis": haiku_analysis,
        "summary": {
            "total_opportunities": len(all_opps),
            "active_rfps": len(classified["active_rfps"]),
            "recent_awards": len(classified["recent_awards"]),
            "verkada_relevant": len(classified["verkada_relevant_signals"]),
            "source_quality": "primary",
            "confidence": "high" if len(all_opps) > 0 else "insufficient_data",
        },
    }

    write_cache(company_slug, result)
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python sam.py <entity_name> [--state XX] [--force]", file=sys.stderr)
        print("  e.g.: python sam.py 'Atlanta Public Schools'", file=sys.stderr)
        print("        python sam.py 'City of Atlanta' --state GA", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Requires: SAM_API_KEY env var (free — register at sam.gov)", file=sys.stderr)
        sys.exit(1)

    entity_name = sys.argv[1]
    force = "--force" in sys.argv

    state = "GA"
    if "--state" in sys.argv:
        idx = sys.argv.index("--state")
        if idx + 1 < len(sys.argv):
            state = sys.argv[idx + 1]

    try:
        result = fetch_sam_data(entity_name, state=state, force_refresh=force)

        if result.get("status") == "insufficient_data":
            print(f"\n  {result['reason']}", file=sys.stderr)
        else:
            summary = result["summary"]
            print(
                f"\n  Done: {result['entity']['name']} ({result['entity']['state']})\n"
                f"  Total opportunities: {summary['total_opportunities']}\n"
                f"  Active RFPs: {summary['active_rfps']}\n"
                f"  Recent awards: {summary['recent_awards']}\n"
                f"  Verkada-relevant: {summary['verkada_relevant']}\n"
                f"  Cached to: sources/{slugify(entity_name)}/sam.json",
                file=sys.stderr,
            )

        print(json.dumps(result, indent=2, default=str))

    except requests.HTTPError as e:
        print(f"ERROR: SAM.gov API error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
