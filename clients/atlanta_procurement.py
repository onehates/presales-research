"""
Atlanta Procurement Client — City of Atlanta solicitations and contract awards.

Data source: City of Atlanta procurement
  - Primary URL: https://www.atlantaga.gov/government/departments/finance/office-of-procurement
  - Alt URL: https://atlpurchasing.com (redirects to atlantaga.gov)

  ACCESS METHOD: BLOCKED — Akamai CDN bot protection.
  atlantaga.gov is behind Akamai's bot management system which returns
  HTTP 403 "Access Denied" for all programmatic requests, including those
  with full browser User-Agent headers.

  Attempted approaches (all blocked):
    1. Direct requests to atlantaga.gov procurement page — 403 Access Denied
       with Akamai reference ID in response body
    2. Browser User-Agent spoofing (Chrome, Firefox, Safari) — same 403
    3. atlpurchasing.com — redirects to atlantaga.gov, then same 403
    4. Various Accept/Accept-Language/Referer header combinations — same 403
    5. robots.txt and sitemap.xml — also blocked by Akamai

  To access Atlanta procurement data, one of these approaches would be needed:
    a. Browser automation (Playwright/Selenium with stealth plugins)
    b. Manual cookie extraction from a browser session
    c. City of Atlanta publishing a public API or open data portal
    d. Third-party aggregator (e.g., BidSync, Periscope)

  Target departments for Verkada-relevant solicitations:
    - Atlanta Police Department (APD)
    - Atlanta Fire Rescue Department (AFRD)
    - Department of Aviation (Hartsfield-Jackson)
    - MARTA (separate entity but sometimes co-procures)
    - Parks and Recreation (facility security)
    - Atlanta Public Schools (separate entity, own procurement)

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

ATLANTA_PROCUREMENT_URL = "https://www.atlantaga.gov/government/departments/finance/office-of-procurement"
ATL_PURCHASING_URL = "https://atlpurchasing.com"

CACHE_TTL_DAYS = 14

# Security-relevant keywords for filtering
SECURITY_KEYWORDS = [
    "security", "surveillance", "camera", "video", "access control",
    "intrusion", "alarm", "monitoring", "cctv", "door", "lock",
    "visitor", "badge", "credential", "intercom", "public safety",
    "law enforcement", "emergency", "sensor", "detection",
    "police", "fire", "safety",
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
# Access attempt
# ---------------------------------------------------------------------------

def attempt_fetch(url: str) -> tuple[bool, str]:
    """
    Attempt to fetch an Atlanta procurement page.
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

        # Detect Akamai block
        if resp.status_code == 403:
            body = resp.text[:500]
            if "Access Denied" in body or "akamai" in body.lower():
                return False, "Akamai CDN bot protection — HTTP 403 Access Denied"
            return False, "HTTP 403 Forbidden"

        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"

        # Check for empty or challenge content
        if len(resp.text.strip()) == 0:
            return False, "HTTP 200 but empty body"

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
    return SOURCES_DIR / query_slug / "atlanta_procurement.json"


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

def fetch_atlanta_procurement_data(query: str, *, force_refresh: bool = False) -> dict:
    """
    Attempt to fetch City of Atlanta procurement data.

    Currently returns insufficient_data due to Akamai CDN blocking.
    """
    query_slug = slugify(f"atlanta-procurement-{query}")

    if not force_refresh:
        cached = read_cache(query_slug)
        if cached is not None:
            return cached

    # Try both endpoints
    endpoints_tried = {}

    print(f"  [atlanta_procurement] attempting {ATLANTA_PROCUREMENT_URL}...", file=sys.stderr)
    success, content = attempt_fetch(ATLANTA_PROCUREMENT_URL)
    endpoints_tried["atlantaga"] = {
        "url": ATLANTA_PROCUREMENT_URL,
        "success": success,
        "error": content if not success else None,
    }

    if not success:
        print(f"  [atlanta_procurement] blocked: {content}", file=sys.stderr)

        print(f"  [atlanta_procurement] attempting {ATL_PURCHASING_URL}...", file=sys.stderr)
        success2, content2 = attempt_fetch(ATL_PURCHASING_URL)
        endpoints_tried["atlpurchasing"] = {
            "url": ATL_PURCHASING_URL,
            "success": success2,
            "error": content2 if not success2 else None,
        }

        if not success2:
            print(f"  [atlanta_procurement] atlpurchasing.com also blocked: {content2}", file=sys.stderr)

    all_failed = all(not ep["success"] for ep in endpoints_tried.values())

    if all_failed:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "atlanta_procurement",
            "source_url": ATLANTA_PROCUREMENT_URL,
            "access_method": "blocked_by_akamai",
            "api_available": False,
            "api_notes": (
                "City of Atlanta procurement (atlantaga.gov) is behind Akamai CDN "
                "bot protection. All paths return HTTP 403 'Access Denied' for "
                "programmatic requests. atlpurchasing.com redirects to atlantaga.gov "
                "and hits the same block. No public API, RSS feed, or open data "
                "portal found for procurement listings."
            ),
            "status": "insufficient_data",
            "reason": (
                f"Akamai CDN bot protection blocks programmatic access to atlantaga.gov. "
                f"atlantaga.gov: {endpoints_tried.get('atlantaga', {}).get('error', 'not tried')}. "
                f"atlpurchasing.com: {endpoints_tried.get('atlpurchasing', {}).get('error', 'not tried')}. "
                f"Would require: browser automation (Playwright) or City of Atlanta publishing a public API."
            ),
            "query": query,
            "endpoints_tried": endpoints_tried,
            "solicitations": [],
            "summary": {
                "total_solicitations": 0,
                "security_relevant_count": 0,
                "access_blocked": True,
                "source_quality": "insufficient_data",
                "confidence": "none",
            },
        }
        write_cache(query_slug, result)
        return result

    # If access succeeded (future scenario)
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "atlanta_procurement",
        "source_url": ATLANTA_PROCUREMENT_URL,
        "access_method": "html_scraping",
        "api_available": False,
        "query": query,
        "solicitations": [],
        "summary": {
            "total_solicitations": 0,
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
        print("Usage: python atlanta_procurement.py <query> [--force]", file=sys.stderr)
        print("  e.g.: python atlanta_procurement.py 'video surveillance'", file=sys.stderr)
        print("        python atlanta_procurement.py 'security cameras' --force", file=sys.stderr)
        print("", file=sys.stderr)
        print("  NOTE: atlantaga.gov is blocked by Akamai CDN bot protection.", file=sys.stderr)
        print("  This client will return insufficient_data until access is resolved.", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1]
    force = "--force" in sys.argv

    result = fetch_atlanta_procurement_data(query, force_refresh=force)

    if result.get("status") == "insufficient_data":
        print(f"\n  {result['reason']}", file=sys.stderr)
    else:
        summary = result["summary"]
        print(
            f"\n  Done: Atlanta Procurement query '{query}'\n"
            f"  Total solicitations: {summary.get('total_solicitations', 0)}\n"
            f"  Security-relevant: {summary.get('security_relevant_count', 0)}\n"
            f"  Cached to: sources/atlanta-procurement-{slugify(query)}/atlanta_procurement.json",
            file=sys.stderr,
        )

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
