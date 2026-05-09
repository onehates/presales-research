"""
Georgia Procurement Registry Client — State-level solicitations and awarded contracts.

Data source: Georgia Procurement Registry (GPR) / DOAS State Purchasing
  - Primary URL: https://ssl.doas.state.ga.us/gpr/ (Georgia Procurement Registry)
  - Alt URL: https://team.georgia.gov (Georgia Technology Enterprise)
  - Alt URL: https://georgia.opengov.com/procurementregistry (OpenGov portal)

  ACCESS METHOD: BLOCKED — All three endpoints are inaccessible programmatically.

  Attempted approaches (all failed):
    1. ssl.doas.state.ga.us/gpr/ — Redirects to /gpr/unsupported?browser= regardless
       of User-Agent headers (Chrome, Firefox, curl). The GPR application appears to
       require a specific Java applet or legacy browser plugin. Even full Chrome UA
       strings trigger the "unsupported browser" redirect.
    2. team.georgia.gov — Returns HTTP 200 but with 0 bytes of content (empty body).
       Tested with multiple User-Agent strings and Accept headers. Consistently empty.
    3. georgia.opengov.com/procurementregistry — Returns HTTP 404. The OpenGov
       procurement registry path does not resolve. The domain exists but this specific
       path is not served.

  To access Georgia procurement data, one of these approaches would be needed:
    a. Browser automation (Playwright/Selenium) for ssl.doas.state.ga.us/gpr/
    b. Georgia publishing a public API or data feed (none found as of 2026-05)
    c. Third-party aggregator that indexes Georgia state solicitations
    d. Manual data entry from browser sessions

  The GPR is the authoritative source for Georgia state-level procurement. It covers:
    - Active solicitations (RFPs, RFQs, ITBs)
    - Awarded contracts with vendor details
    - Statewide contracts available to all state/local agencies
    - Department of Administrative Services (DOAS) purchasing

Extraction: N/A (no data access)
Cache TTL: 14 days (would apply when data access is available)
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "sources"
PERSONA_PATH = PROJECT_ROOT / "persona" / "verkada-se.yml"

HAIKU_MODEL = "claude-haiku-4-5-20251001"

GPR_URL = "https://ssl.doas.state.ga.us/gpr/"
TEAM_GA_URL = "https://team.georgia.gov"
OPENGOV_URL = "https://georgia.opengov.com/procurementregistry"

CACHE_TTL_DAYS = 14

# Security-relevant keywords for filtering solicitations
SECURITY_KEYWORDS = [
    "security", "surveillance", "camera", "video", "access control",
    "intrusion", "alarm", "monitoring", "cctv", "door", "lock",
    "visitor", "badge", "credential", "intercom", "public safety",
    "law enforcement", "emergency", "sensor", "detection",
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
# Access attempts
# ---------------------------------------------------------------------------

def attempt_gpr(url: str) -> tuple[bool, str]:
    """
    Attempt to fetch a Georgia procurement page.
    Returns (success, content_or_error).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)

        # GPR-specific: detect unsupported browser redirect
        if "unsupported" in resp.url.lower():
            return False, "GPR redirected to 'unsupported browser' page — requires legacy browser/plugin"

        # Empty body check (team.georgia.gov pattern)
        if resp.status_code == 200 and len(resp.text.strip()) == 0:
            return False, "HTTP 200 but empty body (0 bytes content)"

        # GPR SPA shell detection: page loads but data is JS-rendered
        content = resp.text
        if resp.status_code == 200 and ("loadingDiv" in content or "appModel" in content):
            return False, "GPR returns JS-rendered SPA shell — procurement data requires browser execution (Angular/JS app)"

        if resp.status_code == 404:
            return False, "HTTP 404 Not Found"
        if resp.status_code == 403:
            return False, "HTTP 403 Forbidden"
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"

        return True, resp.text
    except requests.RequestException as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def cache_path(query_slug: str) -> Path:
    return SOURCES_DIR / query_slug / "ga_procurement.json"


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

def fetch_ga_procurement_data(query: str, *, force_refresh: bool = False) -> dict:
    """
    Attempt to fetch Georgia procurement data.

    Currently returns insufficient_data due to all three endpoints being
    inaccessible programmatically.
    """
    query_slug = slugify(f"ga-procurement-{query}")

    if not force_refresh:
        cached = read_cache(query_slug)
        if cached is not None:
            return cached

    # Try all three endpoints
    endpoints_tried = {}

    print(f"  [ga_procurement] attempting GPR at {GPR_URL}...", file=sys.stderr)
    success, content = attempt_gpr(GPR_URL)
    endpoints_tried["gpr"] = {"url": GPR_URL, "success": success, "error": content if not success else None}

    if not success:
        print(f"  [ga_procurement] GPR blocked: {content}", file=sys.stderr)

        print(f"  [ga_procurement] attempting {TEAM_GA_URL}...", file=sys.stderr)
        success2, content2 = attempt_gpr(TEAM_GA_URL)
        # team.georgia.gov returns a generic GTA homepage, not procurement data
        if success2 and "procurement" not in content2.lower()[:5000]:
            success2 = False
            content2 = "Returns GTA homepage — no procurement listing data"
        endpoints_tried["team_georgia"] = {"url": TEAM_GA_URL, "success": success2, "error": content2 if not success2 else None}

        if not success2:
            print(f"  [ga_procurement] team.georgia.gov: {content2}", file=sys.stderr)

            print(f"  [ga_procurement] attempting {OPENGOV_URL}...", file=sys.stderr)
            success3, content3 = attempt_gpr(OPENGOV_URL)
            endpoints_tried["opengov"] = {"url": OPENGOV_URL, "success": success3, "error": content3 if not success3 else None}

            if not success3:
                print(f"  [ga_procurement] opengov blocked: {content3}", file=sys.stderr)

    # All endpoints failed
    all_failed = all(not ep["success"] for ep in endpoints_tried.values())

    if all_failed:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "ga_procurement",
            "source_url": GPR_URL,
            "access_method": "blocked",
            "api_available": False,
            "api_notes": (
                "Georgia Procurement Registry (GPR) at ssl.doas.state.ga.us/gpr/ "
                "redirects to 'unsupported browser' page for all programmatic requests. "
                "team.georgia.gov returns HTTP 200 with empty body (0 bytes). "
                "georgia.opengov.com/procurementregistry returns 404. "
                "No public API, RSS feed, or machine-readable data format found. "
                "Access requires browser automation (Playwright/Selenium) or manual extraction."
            ),
            "status": "insufficient_data",
            "reason": (
                f"All three Georgia procurement endpoints are inaccessible programmatically. "
                f"GPR: {endpoints_tried.get('gpr', {}).get('error', 'not tried')}. "
                f"Team Georgia: {endpoints_tried.get('team_georgia', {}).get('error', 'not tried')}. "
                f"OpenGov: {endpoints_tried.get('opengov', {}).get('error', 'not tried')}. "
                f"Would require: browser automation (Playwright) or Georgia publishing a public API."
            ),
            "query": query,
            "endpoints_tried": endpoints_tried,
            "solicitations": [],
            "contracts": [],
            "summary": {
                "total_solicitations": 0,
                "total_contracts": 0,
                "security_relevant_count": 0,
                "access_blocked": True,
                "source_quality": "insufficient_data",
                "confidence": "none",
            },
        }
        write_cache(query_slug, result)
        return result

    # If any endpoint succeeded (future scenario), parse and return
    # This code path is not currently reachable
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "ga_procurement",
        "source_url": GPR_URL,
        "access_method": "html_scraping",
        "api_available": False,
        "query": query,
        "solicitations": [],
        "contracts": [],
        "summary": {
            "total_solicitations": 0,
            "total_contracts": 0,
            "security_relevant_count": 0,
            "access_blocked": False,
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
        print("Usage: python ga_procurement.py <query> [--force]", file=sys.stderr)
        print("  e.g.: python ga_procurement.py 'video surveillance'", file=sys.stderr)
        print("        python ga_procurement.py 'Atlanta Public Schools' --force", file=sys.stderr)
        print("", file=sys.stderr)
        print("  NOTE: Georgia procurement endpoints are currently inaccessible.", file=sys.stderr)
        print("  GPR requires legacy browser, team.georgia.gov returns empty,", file=sys.stderr)
        print("  OpenGov procurement registry returns 404.", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1]
    force = "--force" in sys.argv

    result = fetch_ga_procurement_data(query, force_refresh=force)

    if result.get("status") == "insufficient_data":
        print(f"\n  {result['reason']}", file=sys.stderr)
    else:
        summary = result["summary"]
        print(
            f"\n  Done: GA Procurement query '{query}'\n"
            f"  Total solicitations: {summary.get('total_solicitations', 0)}\n"
            f"  Total contracts: {summary.get('total_contracts', 0)}\n"
            f"  Security-relevant: {summary.get('security_relevant_count', 0)}\n"
            f"  Cached to: sources/ga-procurement-{slugify(query)}/ga_procurement.json",
            file=sys.stderr,
        )

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
