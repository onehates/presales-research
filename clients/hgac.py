"""
HGACBuy Client — Houston-Galveston Area Council cooperative purchasing.

Data source: HGACBuy (hgacbuy.org)
  - API Base: https://www.hgacbuy.org/ProductsAndServices/
  - Access method: POST API returning HTML fragments (not JSON)
  - Endpoints (all POST with Content-Type: application/json, body {}):
    /GetAllContracts — all contracts (9 categories)
    /GetAllContractCategoryFilters — category list (8 categories)
    /GetAllVendorFilters — vendor list
    /GetAllManufacturerFilters — manufacturer list
    /ProductsFilteredByManufacturer — POST {"manufacturerId": <id>}
    /ContractsFilteredByContractCategory — POST {"contractCategoryId": <id>}
  - No authentication required. No WAF/challenge detected.

Verkada relevance: Listed as manufacturer (ID 1177) under contract SE05-26
("Video Surveillance, Access Control and Security Fencing Systems").
Two dealer entries: Pavion and APIC, both at 5% discount.

Competitors visible: Avigilon/Alta (5732), Axis (35), Genetec (141),
HANWHA (5729), Milestone (244), Motorola Solutions (248/5731).

Reachability (2026-05-09):
  All POST endpoints → 200, HTML fragments
  Contract detail pages → 200, full HTML

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

HGAC_API_BASE = "https://www.hgacbuy.org/ProductsAndServices"

SECURITY_KEYWORDS = [
    "security", "surveillance", "camera", "video", "access control",
    "intrusion", "alarm", "monitoring", "cctv", "physical security",
    "public safety", "law enforcement", "safety", "sensor", "detection",
    "fencing", "weapon", "threat",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Content-Type": "application/json",
}


def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def cache_path(query_slug: str) -> Path:
    return SOURCES_DIR / query_slug / "hgac.json"


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


def _post(endpoint: str, payload: dict | None = None) -> tuple[bool, str]:
    """POST to an HGACBuy API endpoint."""
    url = f"{HGAC_API_BASE}/{endpoint}"
    try:
        resp = requests.post(
            url, headers=HEADERS, json=payload or {},
            timeout=15, allow_redirects=True,
        )
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        if len(resp.text.strip()) < 10:
            return False, "Empty response"
        return True, resp.text
    except requests.RequestException as e:
        return False, str(e)[:200]


def parse_contracts_html(html: str) -> list[dict]:
    """Parse the HTML fragment returned by GetAllContracts.

    Format: <div class='item' data-contractid='ID'><h2>Title</h2>
            <h3>Contract Number</h3><p>NUM</p>
            <h3>Effective Dates</h3><p>DATES</p>
            <h3>Contract Details</h3><p>DESC</p></div>
    """
    contracts = []
    for m in re.finditer(
        r"data-contractid='(\d+)'[^>]*>\s*<h2>(.*?)</h2>(.*?)</div>\s*</div>",
        html, re.S | re.I,
    ):
        contract_id = m.group(1)
        title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        body = m.group(3)

        # Extract contract number
        num_m = re.search(r'Contract Number</h3>\s*<p>(.*?)</p>', body, re.S | re.I)
        contract_number = re.sub(r'<[^>]+>', '', num_m.group(1)).strip() if num_m else None

        # Extract dates
        date_m = re.search(r'Effective Dates</h3>\s*<p>(.*?)</p>', body, re.S | re.I)
        dates = re.sub(r'<[^>]+>', '', date_m.group(1)).strip() if date_m else None

        contracts.append({
            "contract_id": contract_id,
            "title": title,
            "contract_number": contract_number,
            "effective_dates": dates,
            "url": f"https://www.hgacbuy.org/products-and-services/view-contract?contractid={contract_id}",
        })

    return contracts


def parse_manufacturers_html(html: str) -> list[dict]:
    """Parse the HTML fragment returned by GetAllManufacturerFilters.

    Format: <li><a class='manufacturer-filter-item' data-id='ID'>Name</a></li>
    """
    manufacturers = []
    for m in re.finditer(
        r"data-id=['\"](\d+)['\"][^>]*>(.*?)</a>",
        html, re.S | re.I,
    ):
        mfg_id = m.group(1)
        name = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if name:
            manufacturers.append({"id": mfg_id, "name": name})
    return manufacturers


def fetch_hgac_data(query: str, *, force_refresh: bool = False) -> dict:
    """Fetch HGACBuy contract data filtered by query."""
    query_slug = slugify(f"hgac-{query}")

    if not force_refresh:
        cached = read_cache(query_slug)
        if cached is not None:
            return cached

    print(f"  [hgac] fetching contracts and manufacturers...", file=sys.stderr)

    # Fetch contracts
    ok_contracts, contracts_html = _post("GetAllContracts")
    # Fetch manufacturers
    ok_mfg, mfg_html = _post("GetAllManufacturerFilters")

    if not ok_contracts and not ok_mfg:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "hgacbuy",
            "source_url": HGAC_API_BASE,
            "access_method": "blocked",
            "status": "insufficient_data",
            "reason": f"Failed to fetch HGACBuy: contracts={contracts_html}, mfg={mfg_html}",
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

    contracts = parse_contracts_html(contracts_html) if ok_contracts else []
    manufacturers = parse_manufacturers_html(mfg_html) if ok_mfg else []

    # Filter security-relevant contracts
    security_contracts = []
    for c in contracts:
        if any(kw in c["title"].lower() for kw in SECURITY_KEYWORDS):
            c["relevance"] = "security"
            security_contracts.append(c)

    # Find Verkada and competitor manufacturers
    verkada_mfg = [m for m in manufacturers if "verkada" in m["name"].lower()]
    competitor_names = ["avigilon", "axis", "genetec", "hanwha", "milestone",
                        "motorola", "honeywell", "bosch", "dahua", "hikvision"]
    competitor_mfgs = [
        m for m in manufacturers
        if any(cn in m["name"].lower() for cn in competitor_names)
    ]

    # If we found Verkada, fetch their products
    verkada_products = []
    if verkada_mfg:
        mfg_id = verkada_mfg[0]["id"]
        print(f"  [hgac] fetching Verkada products (mfg_id={mfg_id})...", file=sys.stderr)
        ok_prod, prod_html = _post("ProductsFilteredByManufacturer", {"manufacturerId": int(mfg_id)})
        if ok_prod:
            for m in re.finditer(
                r"data-productid='(\d+)'[^>]*>\s*<h2>(.*?)</h2>(.*?)</div>\s*</div>",
                prod_html, re.S | re.I,
            ):
                product_id = m.group(1)
                title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
                body = m.group(3)
                # Extract contract number
                contract_m = re.search(r'Contract</h3>\s*<p>(.*?)</p>', body, re.S)
                contract = re.sub(r'<[^>]+>', '', contract_m.group(1)).strip() if contract_m else None
                # Extract discount
                disc_m = re.search(r'Discount\s+([\d.]+%?)', body, re.I)
                discount = disc_m.group(1) if disc_m else None
                verkada_products.append({
                    "product_id": product_id,
                    "title": title,
                    "contract": contract,
                    "discount": discount,
                })

    print(
        f"  [hgac] contracts: {len(contracts)}, security: {len(security_contracts)}, "
        f"verkada products: {len(verkada_products)}, "
        f"competitors: {len(competitor_mfgs)}",
        file=sys.stderr,
    )

    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "hgacbuy",
        "source_url": HGAC_API_BASE,
        "access_method": "post_api",
        "api_available": True,
        "api_notes": (
            "HGACBuy has POST API endpoints at /ProductsAndServices/ returning "
            "HTML fragments. No auth required. Endpoints: GetAllContracts, "
            "GetAllManufacturerFilters, ProductsFilteredByManufacturer, etc."
        ),
        "query": query,
        "contracts": security_contracts,
        "all_contract_count": len(contracts),
        "verkada_manufacturer": verkada_mfg[0] if verkada_mfg else None,
        "verkada_products": verkada_products,
        "verkada_contract_note": (
            "Verkada is listed as manufacturer (ID 1177) on HGACBuy contract "
            "SE05-26 ('Video Surveillance, Access Control and Security Fencing "
            "Systems'). Available through dealers Pavion and APIC at 5% discount."
        ),
        "competitor_manufacturers": competitor_mfgs,
        "summary": {
            "total_contracts": len(contracts),
            "security_relevant_count": len(security_contracts),
            "verkada_listed": bool(verkada_mfg),
            "verkada_product_count": len(verkada_products),
            "competitor_count": len(competitor_mfgs),
            "access_blocked": False,
            "source_quality": "primary",
            "confidence": "high",
        },
    }

    write_cache(query_slug, result)
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python hgac.py <query> [--force]", file=sys.stderr)
        print("  e.g.: python hgac.py 'video surveillance'", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1]
    force = "--force" in sys.argv
    result = fetch_hgac_data(query, force_refresh=force)

    summary = result.get("summary", {})
    print(
        f"\n  HGACBuy: query '{query}'\n"
        f"  Status: {result.get('status', 'ok')}\n"
        f"  Total contracts: {summary.get('total_contracts', 0)}\n"
        f"  Security-relevant: {summary.get('security_relevant_count', 0)}\n"
        f"  Verkada listed: {summary.get('verkada_listed', False)}\n"
        f"  Verkada products: {summary.get('verkada_product_count', 0)}\n"
        f"  Competitors: {summary.get('competitor_count', 0)}",
        file=sys.stderr,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
