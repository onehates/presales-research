"""
OMNIA Partners Client — Cooperative purchasing contract search.

Data source: OMNIA Partners (formerly US Communities / National IPA)
  - Search URL: https://www.omniapartners.com/solutions/contract-offerings
    ?contracts[search][keyword]=<term>&contracts[search][industry]=3
  - Access method: HTML scraping (TYPO3 CMS, server-rendered with HTMX pagination)
  - Results include structured data-* attributes on <a> tags:
    data-supplier, data-contract, data-contract_number, data-contract_id, data-industry
  - Industry filter: 3 = Government/Public Sector
  - No authentication required.

Verkada relevance: Verkada holds OMNIA contract R250206 — "Weapons and Threat
Detection Equipment, Services, and Other Solutions" (April 2025 – March 2028,
two 1-year renewal options through March 2030).

Competitors visible: Genetec (R250204), Siemens (2023003490), Everon (R220701),
AVI-SPL (2019.001535).

Reachability (2026-05-09):
  Search endpoint → 200, server-rendered HTML with data attributes
  Supplier detail: /suppliers/verkada/public-sector → 200, 113KB
  Excel download available via contracts[action]=download parameter

Cache TTL: 14 days
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "sources"

CACHE_TTL_DAYS = 14

OMNIA_SEARCH_URL = "https://www.omniapartners.com/solutions/contract-offerings"
OMNIA_INDUSTRY_PUBLIC_SECTOR = "3"

SECURITY_KEYWORDS = [
    "security", "surveillance", "camera", "video", "access control",
    "intrusion", "alarm", "monitoring", "cctv", "physical security",
    "public safety", "law enforcement", "safety", "sensor", "detection",
    "weapon", "threat", "emergency",
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


def fetch_search_page(keyword: str) -> tuple[bool, str]:
    """Fetch the OMNIA contract search results for a keyword."""
    params = {
        "contracts[search][keyword]": keyword,
        "contracts[search][industry]": OMNIA_INDUSTRY_PUBLIC_SECTOR,
    }
    url = f"{OMNIA_SEARCH_URL}?{urlencode(params)}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        if len(resp.text.strip()) < 500:
            return False, "Empty or insufficient content"
        return True, resp.text
    except requests.RequestException as e:
        return False, str(e)[:200]


def parse_search_results(html: str) -> list[dict]:
    """Parse OMNIA search results using structured data-* attributes on <a> tags."""
    contracts = []
    seen = set()

    # Look for <a> tags with data-supplier, data-contract, data-contract_number
    for m in re.finditer(
        r'<a\s+([^>]*data-(?:supplier|contract|contract_number)[^>]*)>(.*?)</a>',
        html, re.S | re.I,
    ):
        attrs_str = m.group(1)
        link_text = re.sub(r'<[^>]+>', '', m.group(2)).strip()

        # Extract data attributes
        supplier = _attr(attrs_str, "data-supplier")
        contract_name = _attr(attrs_str, "data-contract")
        contract_number = _attr(attrs_str, "data-contract_number")
        contract_id = _attr(attrs_str, "data-contract_id")
        industry = _attr(attrs_str, "data-industry")
        href = _attr(attrs_str, "href")

        if not contract_number and not contract_name:
            continue

        key = contract_number or contract_name
        if key in seen:
            continue
        seen.add(key)

        entry = {
            "supplier": supplier,
            "contract_name": contract_name or link_text,
            "contract_number": contract_number,
            "contract_id": contract_id,
            "industry": industry,
            "url": href if href and href.startswith("http") else (
                f"https://www.omniapartners.com{href}" if href else None
            ),
        }
        contracts.append(entry)

    # Fallback: parse links if no data attributes found
    if not contracts:
        for m in re.finditer(
            r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', html, re.S
        ):
            url, text = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if not text or len(text) < 3:
                continue
            entry = {
                "contract_name": text,
                "url": url if url.startswith("http") else f"https://www.omniapartners.com{url}",
            }
            text_lower = text.lower()
            if any(kw in text_lower for kw in SECURITY_KEYWORDS):
                entry["relevance"] = "security"
                contracts.append(entry)

    return contracts


def _attr(attrs_str: str, attr_name: str) -> str | None:
    """Extract an attribute value from an attribute string."""
    m = re.search(rf'{attr_name}="([^"]*)"', attrs_str)
    return m.group(1) if m else None


def fetch_omnia_data(query: str, *, force_refresh: bool = False) -> dict:
    """Fetch OMNIA Partners contract data filtered by query."""
    query_slug = slugify(f"omnia-{query}")

    if not force_refresh:
        cached = read_cache(query_slug)
        if cached is not None:
            return cached

    print(f"  [omnia] searching contracts: '{query}'...", file=sys.stderr)
    success, content = fetch_search_page(query)

    if not success:
        print(f"  [omnia] failed: {content}", file=sys.stderr)
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "omnia_partners",
            "source_url": OMNIA_SEARCH_URL,
            "access_method": "blocked",
            "status": "insufficient_data",
            "reason": f"Failed to fetch OMNIA search results: {content}",
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

    contracts = parse_search_results(content)

    # Tag security-relevant contracts
    security_relevant = []
    for c in contracts:
        name_lower = (c.get("contract_name") or "").lower()
        supplier_lower = (c.get("supplier") or "").lower()
        combined = f"{name_lower} {supplier_lower}"
        if any(kw in combined for kw in SECURITY_KEYWORDS):
            c["relevance"] = "security"
            security_relevant.append(c)

    # Check for Verkada specifically
    verkada_contracts = [
        c for c in contracts
        if "verkada" in (c.get("supplier") or "").lower()
    ]

    print(
        f"  [omnia] found {len(contracts)} contracts, "
        f"security-relevant: {len(security_relevant)}, "
        f"verkada: {len(verkada_contracts)}",
        file=sys.stderr,
    )

    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "omnia_partners",
        "source_url": OMNIA_SEARCH_URL,
        "access_method": "html_scraping",
        "api_available": False,
        "api_notes": (
            "OMNIA Partners search at /solutions/contract-offerings accepts "
            "keyword and industry filter params. Results are server-rendered HTML "
            "with structured data-* attributes. Industry 3 = Public Sector."
        ),
        "query": query,
        "contracts": contracts,
        "security_relevant": security_relevant,
        "verkada_contracts": verkada_contracts,
        "verkada_contract_note": (
            "Verkada holds OMNIA contract R250206 — 'Weapons and Threat Detection "
            "Equipment, Services, and Other Solutions' (April 2025 – March 2028, "
            "two 1-year renewal options through March 2030). Public-sector buyers "
            "can purchase through this vehicle without a full RFP process."
        ),
        "summary": {
            "total_contracts": len(contracts),
            "security_relevant_count": len(security_relevant),
            "verkada_count": len(verkada_contracts),
            "access_blocked": False,
            "source_quality": "primary",
            "confidence": "high",
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
        f"  Total contracts: {summary.get('total_contracts', 0)}\n"
        f"  Security-relevant: {summary.get('security_relevant_count', 0)}\n"
        f"  Verkada contracts: {summary.get('verkada_count', 0)}",
        file=sys.stderr,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
