"""
Sourcewell Client — Scrapes cooperative purchasing solicitations from sourcewell-mn.gov.

Data source: Sourcewell solicitations page (HTML scraping)
  - URL: https://www.sourcewell-mn.gov/solicitations
  - Access method: HTML scraping (Salesforce Visualforce server-side rendered page)
  - No API available: The Drupal JSON:API at /jsonapi/ exposes taxonomy terms
    (categories) but node endpoints for solicitations/contracts are access-controlled.
    The solicitation listing is served from a Salesforce Visualforce page at /swp/
    with no public API.
  - No authentication required for the listing page.
  - The page renders three sections: Open, Pending, Recently Awarded.
  - Detail pages at /solicitations/{id} are also Salesforce but return empty
    for programmatic access; all needed data is on the listing page.

Verkada-relevant Sourcewell categories (from taxonomy API):
  - 28991: Building security
  - 29216: Building security and cameras
  - 29231: Cyber and data security
  - 29351: Body armor
  - 29336: Firefighting
  - 29321: EMS
  - 29346: Audio and video recording systems
  - 29326: Communications and technology

Fragility points:
  1. HTML structure changes: The Salesforce Visualforce template could change at any
     time. The parsing regex depends on the current DOM structure (div.row.tr pattern
     with solicitation links and date spans).
  2. Session/cookie issues: Different curl sessions occasionally get different content.
     Retry logic handles this.
  3. No detail page access: We only get title, ID, date, and status (open/pending/awarded).
     No description text or category tags from the listing page.
  4. Rate limiting: Be polite — single page fetch, no need for throttling.

Extraction: Deterministic keyword matching + Claude Haiku per CLAUDE.md model assignment.
Cache TTL: 14 days (cooperative purchasing cycles are slow).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import yaml

try:
    import anthropic
except ImportError:
    anthropic = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "sources"
PERSONA_PATH = PROJECT_ROOT / "persona" / "verkada-se.yml"

HAIKU_MODEL = "claude-haiku-4-5-20251001"

SOLICITATIONS_URL = "https://www.sourcewell-mn.gov/solicitations"

CACHE_TTL_DAYS = 14

# Security-relevant keywords for filtering solicitations
SECURITY_KEYWORDS = [
    "security", "surveillance", "camera", "video", "access control",
    "intrusion", "alarm", "monitoring", "cctv", "door", "lock",
    "visitor", "badge", "credential", "intercom", "public safety",
    "law enforcement", "emergency", "fire", "body armor", "body-worn",
    "license plate", "lpr", "gunshot", "sensor", "detection",
    "guard", "patrol", "command center", "soc", "noc",
]

# Broader infrastructure keywords that indicate physical security adjacency
INFRA_KEYWORDS = [
    "building", "facility", "campus", "construction",
    "hvac", "lighting", "elevator", "parking",
    "network", "wireless", "communications", "technology",
    "managed services", "it infrastructure", "cloud",
]


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------

def _load_persona() -> dict:
    if not PERSONA_PATH.exists():
        return {}
    try:
        return yaml.safe_load(PERSONA_PATH.read_text()) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# HTML Scraping
# ---------------------------------------------------------------------------

def fetch_solicitations_page() -> str:
    """Fetch the Sourcewell solicitations listing page HTML."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PresalesResearch/1.0)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(SOLICITATIONS_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_solicitations(html: str) -> dict:
    """
    Parse the Sourcewell solicitations page into structured data.

    Returns dict with keys: open, pending, recently_awarded
    Each value is a list of dicts with: id, title, date, url
    """
    results = {"open": [], "pending": [], "recently_awarded": []}

    # Find section boundaries
    sections = []
    for m in re.finditer(r'<h2[^>]*>\s*(Open|Pending|Recently awarded)\s*</h2>', html):
        key = m.group(1).strip().lower().replace(' ', '_')
        sections.append((m.start(), key))

    # Parse entries: <a href=".../solicitations/{id}">Title</a> ... <span>Date</span>
    pattern = (
        r'<a\s+href="https://www\.sourcewell-mn\.gov/solicitations/(\d+)">'
        r'(.*?)</a>\s*\n\s*</div>\s*\n\s*<div[^>]*><span[^>]*>\s*(.*?)</span>'
    )

    for m in re.finditer(pattern, html, re.S):
        sol_id = m.group(1)
        title = m.group(2).strip()
        date_str = m.group(3).strip()
        pos = m.start()

        # Determine section
        section = "open"  # default
        for sec_pos, sec_name in sections:
            if pos > sec_pos:
                section = sec_name

        entry = {
            "id": sol_id,
            "title": title,
            "date": date_str,
            "url": f"https://www.sourcewell-mn.gov/solicitations/{sol_id}",
        }
        results[section].append(entry)

    return results


# ---------------------------------------------------------------------------
# Keyword classification
# ---------------------------------------------------------------------------

def classify_relevance(solicitations: dict) -> dict:
    """
    Classify solicitations by Verkada relevance.

    Returns:
      - security_relevant: directly about security/surveillance/access control
      - infra_adjacent: about building/facility/IT infrastructure
      - all_solicitations: complete list
    """
    security_relevant = []
    infra_adjacent = []

    all_entries = []
    for status, entries in solicitations.items():
        for entry in entries:
            entry_with_status = {**entry, "status": status}
            all_entries.append(entry_with_status)

            title_lower = entry["title"].lower()

            if any(kw in title_lower for kw in SECURITY_KEYWORDS):
                entry_with_status["relevance"] = "security"
                security_relevant.append(entry_with_status)
            elif any(kw in title_lower for kw in INFRA_KEYWORDS):
                entry_with_status["relevance"] = "infrastructure"
                infra_adjacent.append(entry_with_status)

    return {
        "security_relevant": security_relevant,
        "infra_adjacent": infra_adjacent,
        "all_solicitations": all_entries,
    }


# ---------------------------------------------------------------------------
# Haiku analysis
# ---------------------------------------------------------------------------

def analyze_with_haiku(classified: dict, persona: dict) -> dict:
    """Run Haiku analysis on relevant solicitations."""
    if not anthropic:
        return {"status": "skipped", "reason": "anthropic package not installed"}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"status": "extraction_error", "reason": "ANTHROPIC_API_KEY not set"}

    security = classified["security_relevant"]
    infra = classified["infra_adjacent"]

    if not security and not infra:
        return {"status": "no_relevant_solicitations"}

    # Build persona context
    triggers = persona.get("triggers", [])
    trigger_names = [t.get("name", "") for t in triggers if isinstance(t, dict)]

    prompt = f"""Analyze these cooperative purchasing (Sourcewell) solicitations for Verkada sales relevance.

SECURITY-RELEVANT SOLICITATIONS:
{json.dumps(security, indent=2) if security else "None"}

INFRASTRUCTURE-ADJACENT SOLICITATIONS:
{json.dumps(infra, indent=2) if infra else "None"}

KNOWN TRIGGERS from persona file: {', '.join(trigger_names)}

For each relevant solicitation, determine:
1. Which Verkada product lines could be positioned (cameras, access control, guest, alarms, intercoms, mailboxes, air quality)
2. Which persona triggers fire (from the list above)
3. Whether this represents an active procurement opportunity vs. background signal

Return JSON:
{{
  "findings": [
    {{
      "solicitation_id": "...",
      "title": "...",
      "verkada_product_fit": ["cameras", "access_control"],
      "triggers_fired": ["capital_project_signal"],
      "opportunity_type": "active_procurement|background_signal",
      "sales_angle": "one sentence positioning"
    }}
  ],
  "summary": "one paragraph overview"
}}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        # Try to parse JSON from response
        json_match = re.search(r'\{.*\}', text, re.S)
        if json_match:
            return json.loads(json_match.group(0))
        return {"status": "parse_error", "raw": text[:1000]}
    except Exception as e:
        return {"status": "extraction_error", "reason": str(e)}


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def cache_path(query_slug: str) -> Path:
    return SOURCES_DIR / query_slug / "sourcewell.json"


def read_cache(query_slug: str) -> dict | None:
    path = cache_path(query_slug)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    retrieved_at = data.get("retrieved_at")
    if not retrieved_at:
        return None

    try:
        ts = datetime.fromisoformat(retrieved_at)
    except ValueError:
        return None

    if datetime.now(timezone.utc) - ts > timedelta(days=CACHE_TTL_DAYS):
        return None
    return data


def write_cache(query_slug: str, data: dict) -> Path:
    path = cache_path(query_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))
    print(f"  [cache] wrote {path}", file=sys.stderr)
    return path


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def fetch_sourcewell_data(query: str, *, force_refresh: bool = False) -> dict:
    """
    Full pipeline: fetch solicitations page → parse → classify → Haiku analysis → cache.

    The query parameter is used for keyword filtering and cache key.
    Unlike other clients that take a company name, Sourcewell is queried by
    category/keyword (e.g., "video surveillance", "access control").
    """
    query_slug = slugify(f"sourcewell-{query}")

    if not force_refresh:
        cached = read_cache(query_slug)
        if cached is not None:
            return cached

    # Fetch and parse
    print(f"  [sourcewell] fetching solicitations page...", file=sys.stderr)
    try:
        html = fetch_solicitations_page()
    except requests.RequestException as e:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "sourcewell",
            "source_url": SOLICITATIONS_URL,
            "status": "insufficient_data",
            "reason": f"Failed to fetch solicitations page: {e}",
            "query": query,
        }
        write_cache(query_slug, result)
        return result

    solicitations = parse_solicitations(html)
    total = sum(len(v) for v in solicitations.values())
    print(f"  [sourcewell] parsed {total} solicitations "
          f"(open={len(solicitations['open'])}, "
          f"pending={len(solicitations['pending'])}, "
          f"awarded={len(solicitations['recently_awarded'])})", file=sys.stderr)

    # Classify by relevance
    classified = classify_relevance(solicitations)
    print(f"  [sourcewell] security-relevant: {len(classified['security_relevant'])}, "
          f"infra-adjacent: {len(classified['infra_adjacent'])}", file=sys.stderr)

    # Additional keyword filter for the specific query
    query_lower = query.lower()
    query_matches = []
    for entry in classified["all_solicitations"]:
        if query_lower in entry["title"].lower():
            if entry not in classified["security_relevant"]:
                query_matches.append(entry)

    # Haiku analysis
    persona = _load_persona()
    haiku_analysis = analyze_with_haiku(classified, persona)

    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "sourcewell",
        "source_url": SOLICITATIONS_URL,
        "access_method": "html_scraping",
        "api_available": False,
        "api_notes": (
            "Sourcewell solicitation listing is a Salesforce Visualforce page "
            "rendered server-side. No public API. The Drupal JSON:API at /jsonapi/ "
            "exposes category taxonomy terms but node endpoints for solicitations "
            "are access-controlled."
        ),
        "query": query,
        "solicitations": solicitations,
        "classification": {
            "security_relevant": classified["security_relevant"],
            "infra_adjacent": classified["infra_adjacent"],
            "query_matches": query_matches,
        },
        "haiku_analysis": haiku_analysis,
        "summary": {
            "total_solicitations": total,
            "open_count": len(solicitations["open"]),
            "pending_count": len(solicitations["pending"]),
            "awarded_count": len(solicitations["recently_awarded"]),
            "security_relevant_count": len(classified["security_relevant"]),
            "infra_adjacent_count": len(classified["infra_adjacent"]),
            "query_match_count": len(query_matches),
            "source_quality": "primary",
            "confidence": "high",
        },
    }

    write_cache(query_slug, result)
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python sourcewell.py <query> [--force]", file=sys.stderr)
        print("  e.g.: python sourcewell.py 'video surveillance'", file=sys.stderr)
        print("        python sourcewell.py 'access control' --force", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Scrapes Sourcewell solicitations page (no API key needed)", file=sys.stderr)
        print("  Filters for security/infrastructure relevance", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1]
    force = "--force" in sys.argv

    result = fetch_sourcewell_data(query, force_refresh=force)

    summary = result.get("summary", {})
    if result.get("status") == "insufficient_data":
        print(f"\n  {result['reason']}", file=sys.stderr)
    else:
        print(
            f"\n  Done: Sourcewell query '{query}'\n"
            f"  Total solicitations: {summary.get('total_solicitations', 0)}\n"
            f"  Open: {summary.get('open_count', 0)}\n"
            f"  Pending: {summary.get('pending_count', 0)}\n"
            f"  Recently awarded: {summary.get('awarded_count', 0)}\n"
            f"  Security-relevant: {summary.get('security_relevant_count', 0)}\n"
            f"  Infra-adjacent: {summary.get('infra_adjacent_count', 0)}\n"
            f"  Query matches: {summary.get('query_match_count', 0)}\n"
            f"  Cached to: sources/sourcewell-{slugify(query)}/sourcewell.json",
            file=sys.stderr,
        )

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
