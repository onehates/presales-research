"""
OMNIA Partners Client — Cooperative purchasing contract listings.

Data source: OMNIA Partners (formerly US Communities / National IPA)
  - URL: https://www.omniapartners.com/contracts
  - Access method: HTML scraping (server-rendered)
  - The /contracts page returns 200 with a contract portfolio overview.
  - /public-sector/contracts and /public-sector/solicitations both 404.
  - No public API found. The contracts page is a marketing catalog, not a
    searchable solicitation listing.

Verkada relevance: Verkada holds an OMNIA Partners cooperative contract
for video surveillance and physical security. This allows public-sector
buyers to purchase Verkada without a full RFP process.

Reachability (2026-05-09):
  omniapartners.com/contracts → 200, 71KB, server-rendered HTML
  omniapartners.com/public-sector/contracts → 404
  omniapartners.com/public-sector/solicitations → 404
  omniapartners.com/api/* → no API endpoints found

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

OMNIA_CONTRACTS_URL = "https://www.omniapartners.com/contracts"

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
    return SOURCES_DIR / query_slug / "omnia.json"


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


def fetch_contracts_page() -> tuple[bool, str]:
    """Fetch the OMNIA Partners contracts page."""
    try:
        resp = requests.get(OMNIA_CONTRACTS_URL, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        if len(resp.text.strip()) < 500:
            return False, "Empty or insufficient content"
        return True, resp.text
    except requests.RequestException as e:
        return False, str(e)[:200]


def parse_contracts(html: str) -> dict:
    """Parse the OMNIA contracts page for security-relevant contract categories."""
    contracts = []
    security_relevant = []

    # Extract contract/category links and text blocks
    for m in re.finditer(
        r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', html, re.S
    ):
        url, text = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if not text or len(text) < 3:
            continue
        entry = {
            "title": text,
            "url": url if url.startswith("http") else f"https://www.omniapartners.com{url}",
        }
        contracts.append(entry)

        text_lower = text.lower()
        if any(kw in text_lower for kw in SECURITY_KEYWORDS):
            entry["relevance"] = "security"
            security_relevant.append(entry)

    # Also look for contract category cards/sections
    categories = []
    for m in re.finditer(
        r'<(?:h[23456]|div|span)[^>]*class="[^"]*(?:card|category|contract)[^"]*"[^>]*>'
        r'(.*?)</(?:h[23456]|div|span)>',
        html, re.S | re.I,
    ):
        text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        if text and len(text) > 3:
            categories.append(text)

    return {
        "all_contracts": contracts,
        "security_relevant": security_relevant,
        "categories_found": list(set(categories))[:20],
    }


def fetch_omnia_data(query: str, *, force_refresh: bool = False) -> dict:
    """Fetch OMNIA Partners contract data filtered by query."""
    query_slug = slugify(f"omnia-{query}")

    if not force_refresh:
        cached = read_cache(query_slug)
        if cached is not None:
            return cached

    print(f"  [omnia] fetching contracts page...", file=sys.stderr)
    success, content = fetch_contracts_page()

    if not success:
        print(f"  [omnia] failed: {content}", file=sys.stderr)
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "omnia_partners",
            "source_url": OMNIA_CONTRACTS_URL,
            "access_method": "blocked",
            "status": "insufficient_data",
            "reason": f"Failed to fetch OMNIA contracts page: {content}",
            "query": query,
            "contracts": [],
            "security_relevant": [],
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

    parsed = parse_contracts(content)

    # Additional query-specific filtering
    query_lower = query.lower()
    query_matches = [
        c for c in parsed["all_contracts"]
        if query_lower in c["title"].lower()
        and c not in parsed["security_relevant"]
    ]

    print(
        f"  [omnia] parsed {len(parsed['all_contracts'])} contract links, "
        f"security-relevant: {len(parsed['security_relevant'])}",
        file=sys.stderr,
    )

    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "omnia_partners",
        "source_url": OMNIA_CONTRACTS_URL,
        "access_method": "html_scraping",
        "api_available": False,
        "api_notes": (
            "OMNIA Partners has no public API for contract/solicitation search. "
            "The /contracts page is a marketing overview, not a searchable database. "
            "Contract details require navigating to individual vendor pages."
        ),
        "query": query,
        "contracts": parsed["security_relevant"],
        "query_matches": query_matches,
        "categories_found": parsed["categories_found"],
        "verkada_contract_note": (
            "Verkada holds an OMNIA Partners cooperative purchasing contract "
            "for video surveillance and physical security. Public-sector buyers "
            "can purchase through this vehicle without a full RFP process."
        ),
        "summary": {
            "total_contract_links": len(parsed["all_contracts"]),
            "security_relevant_count": len(parsed["security_relevant"]),
            "query_match_count": len(query_matches),
            "categories_count": len(parsed["categories_found"]),
            "access_blocked": False,
            "source_quality": "secondary",
            "confidence": "medium",
        },
    }

    write_cache(query_slug, result)
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python omnia.py <query> [--force]", file=sys.stderr)
        print("  e.g.: python omnia.py 'video surveillance'", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1]
    force = "--force" in sys.argv
    result = fetch_omnia_data(query, force_refresh=force)

    summary = result.get("summary", {})
    print(
        f"\n  OMNIA Partners: query '{query}'\n"
        f"  Status: {result.get('status', 'ok')}\n"
        f"  Contract links: {summary.get('total_contract_links', 0)}\n"
        f"  Security-relevant: {summary.get('security_relevant_count', 0)}",
        file=sys.stderr,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
