"""
COSTARS Client — Pennsylvania cooperative purchasing contract search.

Data source: PA Department of General Services COSTARS Program
  - Contract search requires Keystone Login (PA Commonwealth shared auth).
  - costars.state.pa.us redirects to pa.gov/agencies/dgs/programs-and-services/costars
    (static informational page, no searchable listings).
  - PA eMarketplace (emarketplace.state.pa.us/Search.aspx) is publicly accessible
    and has a "COSTARS" filter in ddlTypes dropdown, but requires ASP.NET
    ViewState/EventValidation tokens for POST search.
  - Overall: contract search is behind auth wall. We can only confirm COSTARS
    program exists and scrape the info page for category/contact details.

Reachability (2026-05-09):
  costars.state.pa.us → 302 chain → pa.gov static info page (200)
  costars.state.pa.us/Login.aspx → 200 (Keystone Login wall)
  emarketplace.state.pa.us/Search.aspx → 200, 138KB (ASP.NET, public)
  dgs.pa.gov/COSTARS/ → 302 chain → pa.gov static info page

Cache TTL: 14 days
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "sources"

CACHE_TTL_DAYS = 14

COSTARS_URLS = [
    "https://www.costars.state.pa.us/",
    "https://www.dgs.pa.gov/COSTARS/Pages/ContractSearch.aspx",
]

SECURITY_KEYWORDS = [
    "security", "surveillance", "camera", "video", "access control",
    "intrusion", "alarm", "monitoring", "cctv", "physical security",
    "public safety", "law enforcement", "safety", "sensor", "detection",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}


def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def cache_path(query_slug: str) -> Path:
    return SOURCES_DIR / query_slug / "costars.json"


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


def fetch_costars_page(url: str) -> tuple[bool, str]:
    """Fetch a COSTARS page."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        if len(resp.text.strip()) < 500:
            return False, "Empty or insufficient content"
        return True, resp.text
    except requests.RequestException as e:
        return False, str(e)[:200]


def parse_costars_contracts(html: str, source_url: str) -> dict:
    """Parse COSTARS pages for security-relevant contract references."""
    contracts = []
    security_relevant = []

    # Extract links with text
    for m in re.finditer(
        r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', html, re.S
    ):
        url, text = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if not text or len(text) < 3:
            continue

        # Resolve relative URLs
        if url.startswith("/"):
            from urllib.parse import urlparse
            parsed = urlparse(source_url)
            url = f"{parsed.scheme}://{parsed.netloc}{url}"

        entry = {"title": text, "url": url}
        contracts.append(entry)

        text_lower = text.lower()
        if any(kw in text_lower for kw in SECURITY_KEYWORDS):
            entry["relevance"] = "security"
            security_relevant.append(entry)

    # Look for contract number patterns (COSTARS-###)
    contract_numbers = re.findall(r'COSTARS[-\s]?\d+', html, re.I)
    contract_numbers = list(set(contract_numbers))[:30]

    # Look for table rows that might contain contract data
    table_entries = []
    for m in re.finditer(
        r'<tr[^>]*>(.*?)</tr>', html, re.S | re.I
    ):
        row_text = re.sub(r'<[^>]+>', ' ', m.group(1)).strip()
        row_lower = row_text.lower()
        if any(kw in row_lower for kw in SECURITY_KEYWORDS):
            table_entries.append(row_text[:200])

    return {
        "all_contracts": contracts,
        "security_relevant": security_relevant,
        "contract_numbers": contract_numbers,
        "security_table_rows": table_entries[:20],
    }


def fetch_costars_data(query: str, *, force_refresh: bool = False) -> dict:
    """Fetch COSTARS contract data filtered by query."""
    query_slug = slugify(f"costars-{query}")

    if not force_refresh:
        cached = read_cache(query_slug)
        if cached is not None:
            return cached

    print(f"  [costars] fetching pages...", file=sys.stderr)

    all_security = []
    all_contract_nums = []
    all_table_rows = []
    pages_fetched = 0
    pages_failed = 0

    for url in COSTARS_URLS:
        success, content = fetch_costars_page(url)
        if not success:
            print(f"  [costars] failed {url}: {content}", file=sys.stderr)
            pages_failed += 1
            continue

        pages_fetched += 1
        parsed = parse_costars_contracts(content, url)
        all_security.extend(parsed["security_relevant"])
        all_contract_nums.extend(parsed["contract_numbers"])
        all_table_rows.extend(parsed["security_table_rows"])

    if pages_fetched == 0:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "costars",
            "source_urls": COSTARS_URLS,
            "access_method": "blocked",
            "status": "insufficient_data",
            "reason": f"Failed to fetch all COSTARS pages",
            "query": query,
            "contracts": [],
            "summary": {
                "total_contracts": 0,
                "security_relevant_count": 0,
                "access_blocked": True,
                "source_quality": "insufficient_data",
                "confidence": "none",
            },
        }
        write_cache(query_slug, result)
        return result

    # Deduplicate
    seen_titles = set()
    deduped_security = []
    for c in all_security:
        if c["title"] not in seen_titles:
            seen_titles.add(c["title"])
            deduped_security.append(c)

    all_contract_nums = list(set(all_contract_nums))

    # Query-specific filtering
    query_lower = query.lower()
    query_matches = [
        c for c in deduped_security
        if query_lower in c["title"].lower()
    ]

    print(
        f"  [costars] fetched {pages_fetched} pages, "
        f"security-relevant: {len(deduped_security)}, "
        f"contract numbers: {len(all_contract_nums)}",
        file=sys.stderr,
    )

    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "costars",
        "source_urls": COSTARS_URLS,
        "access_method": "html_scraping",
        "api_available": False,
        "api_notes": (
            "PA COSTARS has no public API. The portal at costars.state.pa.us "
            "and dgs.pa.gov/COSTARS are ASP.NET WebForms with server-rendered HTML. "
            "Contract search requires form submission with ViewState tokens."
        ),
        "query": query,
        "contracts": deduped_security,
        "query_matches": query_matches,
        "contract_numbers_found": all_contract_nums,
        "security_table_rows": all_table_rows[:10],
        "summary": {
            "pages_fetched": pages_fetched,
            "pages_failed": pages_failed,
            "security_relevant_count": len(deduped_security),
            "query_match_count": len(query_matches),
            "contract_numbers_count": len(all_contract_nums),
            "access_blocked": False,
            "source_quality": "secondary",
            "confidence": "medium",
        },
    }

    write_cache(query_slug, result)
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python costars.py <query> [--force]", file=sys.stderr)
        print("  e.g.: python costars.py 'video surveillance'", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1]
    force = "--force" in sys.argv
    result = fetch_costars_data(query, force_refresh=force)

    summary = result.get("summary", {})
    print(
        f"\n  COSTARS: query '{query}'\n"
        f"  Status: {result.get('status', 'ok')}\n"
        f"  Pages fetched: {summary.get('pages_fetched', 0)}\n"
        f"  Security-relevant: {summary.get('security_relevant_count', 0)}\n"
        f"  Contract numbers: {summary.get('contract_numbers_count', 0)}",
        file=sys.stderr,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
