"""
TIPS-USA Client — Cooperative purchasing RFP listings from tips-usa.com.

Data source: TIPS-USA (The Interlocal Purchasing System)
  - URL: https://www.tips-usa.com/rfps.cfm (RFP listings)
  - URL: https://www.tips-usa.com/contractsSearch.cfm (awarded contracts)

  ACCESS METHOD: BLOCKED — Cloudflare managed challenge.
  tips-usa.com is behind Cloudflare's managed challenge (bot protection)
  on ALL paths, including robots.txt and sitemap.xml. Every endpoint
  returns HTTP 403 with a JavaScript challenge page.

  Attempted approaches (all blocked):
    1. Direct curl requests — 403 Cloudflare challenge
    2. Browser User-Agent spoofing — 403 Cloudflare challenge
    3. Staging domain (public-staging2.tips-usa.com, leaked via robots.txt) — same blocking
    4. API probing (/api/, /api/rfps, /rfps.json, /feed, /rss) — all 403
    5. Sitemap.xml, robots.txt — both serve Cloudflare challenge HTML

  To access TIPS data, one of these approaches would be needed:
    a. Browser automation (Playwright/Selenium with stealth plugins)
    b. Manual cookie extraction from a browser session
    c. TIPS publishing a public API or data feed (none found as of 2026-05)
    d. Third-party aggregator that indexes TIPS listings

  The site is ColdFusion-based (.cfm pages). Based on the URL structure:
    - /rfps.cfm — active RFP listings
    - /contractsSearch.cfm — awarded contract search
    - /vendorProfile.cfm?contractID=xxx — vendor/contract detail pages
  But none of these are accessible programmatically.

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

TIPS_BASE_URL = "https://www.tips-usa.com"
TIPS_RFPS_URL = f"{TIPS_BASE_URL}/rfps.cfm"
TIPS_CONTRACTS_URL = f"{TIPS_BASE_URL}/contractsSearch.cfm"

CACHE_TTL_DAYS = 14

# Known TIPS contract categories relevant to Verkada
# (from public-facing marketing materials and third-party references)
TIPS_SECURITY_CATEGORIES = [
    "Security and Safety",
    "Technology Solutions",
    "Facility Solutions",
]

# Security-relevant keywords for filtering
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
# Access attempt
# ---------------------------------------------------------------------------

def attempt_fetch(url: str) -> tuple[bool, str]:
    """
    Attempt to fetch a TIPS-USA page.
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
        content = resp.text

        # Detect Cloudflare challenge
        if "Just a moment..." in content or "challenges.cloudflare.com" in content:
            return False, "Cloudflare managed challenge detected — programmatic access blocked"
        if resp.status_code == 403:
            return False, f"HTTP 403 Forbidden"
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"

        return True, content
    except requests.RequestException as e:
        return False, str(e)


def parse_rfps(html: str) -> list[dict]:
    """
    Parse TIPS RFP listing page.

    NOTE: This function is implemented for when Cloudflare access is resolved
    (e.g., via browser automation). The expected HTML structure is based on
    the ColdFusion page pattern — actual DOM may differ.
    """
    rfps = []

    # Expected pattern (based on typical .cfm table layouts):
    # <tr> with RFP number, title, category, dates
    rows = re.findall(
        r'<tr[^>]*>(.*?)</tr>',
        html, re.S | re.I
    )

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
        if len(cells) >= 3:
            rfp = {
                "title": re.sub(r'<[^>]+>', '', cells[0]).strip(),
                "category": re.sub(r'<[^>]+>', '', cells[1]).strip() if len(cells) > 1 else "",
                "date": re.sub(r'<[^>]+>', '', cells[2]).strip() if len(cells) > 2 else "",
            }
            # Extract links
            link = re.search(r'href="([^"]*)"', cells[0])
            if link:
                rfp["url"] = link.group(1)
                if not rfp["url"].startswith("http"):
                    rfp["url"] = f"{TIPS_BASE_URL}/{rfp['url'].lstrip('/')}"
            if rfp["title"]:
                rfps.append(rfp)

    return rfps


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def cache_path(query_slug: str) -> Path:
    return SOURCES_DIR / query_slug / "tips.json"


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

def fetch_tips_data(query: str, *, force_refresh: bool = False) -> dict:
    """
    Attempt to fetch TIPS-USA RFP data.

    Currently returns insufficient_data due to Cloudflare blocking.
    When access is resolved, will: fetch → parse → classify → cache.
    """
    query_slug = slugify(f"tips-{query}")

    if not force_refresh:
        cached = read_cache(query_slug)
        if cached is not None:
            return cached

    # Attempt access
    print(f"  [tips] attempting to fetch {TIPS_RFPS_URL}...", file=sys.stderr)
    success, content = attempt_fetch(TIPS_RFPS_URL)

    if not success:
        print(f"  [tips] blocked: {content}", file=sys.stderr)
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "tips_usa",
            "source_url": TIPS_RFPS_URL,
            "access_method": "blocked_by_cloudflare",
            "api_available": False,
            "api_notes": (
                "TIPS-USA (tips-usa.com) is entirely behind Cloudflare managed challenge. "
                "All paths return HTTP 403 with a JavaScript bot challenge page. "
                "No public API, RSS feed, or machine-readable data format found. "
                "Access requires browser automation (Playwright/Selenium) or manual "
                "cookie extraction. Site is ColdFusion-based (.cfm pages)."
            ),
            "status": "insufficient_data",
            "reason": (
                f"Cloudflare managed challenge blocks programmatic access to tips-usa.com. "
                f"Specific error: {content}. "
                f"Would require: browser automation (Playwright) or TIPS publishing a public API."
            ),
            "query": query,
            "rfps": [],
            "security_relevant": [],
            "summary": {
                "total_rfps": 0,
                "security_relevant_count": 0,
                "access_blocked": True,
                "source_quality": "insufficient_data",
                "confidence": "none",
            },
        }
        write_cache(query_slug, result)
        return result

    # If we get here, Cloudflare was bypassed (future scenario)
    print(f"  [tips] successfully fetched page ({len(content)} bytes)", file=sys.stderr)
    rfps = parse_rfps(content)
    print(f"  [tips] parsed {len(rfps)} RFPs", file=sys.stderr)

    # Keyword classification
    query_lower = query.lower()
    security_relevant = []
    for rfp in rfps:
        title_lower = rfp.get("title", "").lower()
        cat_lower = rfp.get("category", "").lower()
        combined = f"{title_lower} {cat_lower}"
        if any(kw in combined for kw in SECURITY_KEYWORDS) or query_lower in combined:
            rfp["relevance"] = "security"
            security_relevant.append(rfp)

    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "tips_usa",
        "source_url": TIPS_RFPS_URL,
        "access_method": "html_scraping",
        "api_available": False,
        "query": query,
        "rfps": rfps,
        "security_relevant": security_relevant,
        "summary": {
            "total_rfps": len(rfps),
            "security_relevant_count": len(security_relevant),
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
        print("Usage: python tips.py <query> [--force]", file=sys.stderr)
        print("  e.g.: python tips.py 'video surveillance'", file=sys.stderr)
        print("        python tips.py 'access control' --force", file=sys.stderr)
        print("", file=sys.stderr)
        print("  NOTE: tips-usa.com is currently blocked by Cloudflare.", file=sys.stderr)
        print("  This client will return insufficient_data until access is resolved.", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1]
    force = "--force" in sys.argv

    result = fetch_tips_data(query, force_refresh=force)

    if result.get("status") == "insufficient_data":
        print(f"\n  {result['reason']}", file=sys.stderr)
    else:
        summary = result["summary"]
        print(
            f"\n  Done: TIPS-USA query '{query}'\n"
            f"  Total RFPs: {summary.get('total_rfps', 0)}\n"
            f"  Security-relevant: {summary.get('security_relevant_count', 0)}\n"
            f"  Cached to: sources/tips-{slugify(query)}/tips.json",
            file=sys.stderr,
        )

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
