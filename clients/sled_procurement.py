"""
Georgia SLED Procurement Client — Multi-source scraper for per-district and
per-county procurement portals relevant to Georgia SLED accounts.

Approach: static mapping of entity → known procurement portal URLs.
For each entity, attempt the URL. If accessible, parse active RFPs and recent
awards filtered for security/surveillance/access-control. If blocked, return
insufficient_data with specific blocking reason.

Reachability audit (2026-05-09):
  ACCESSIBLE:
    - Fulton County Schools → fultonschools.bonfirehub.com (Bonfire JS SPA)
    - Georgia Tech → procurement.gatech.edu (Drupal, server-rendered)
    - Georgia State → finance.gsu.edu/procurement/ (server-rendered)
    - USG Board of Regents → usg.edu/procurement (server-rendered)
    - BidNet Direct Georgia → bidnetdirect.com/georgia (aggregator)
    - GTA Georgia → gta.georgia.gov/about-us/procurement (info, not listings)
  BLOCKED:
    - Atlanta Public Schools → atlantapublicschools.us (Blackboard CMS, all
      procurement paths 404 — district likely uses a third-party vendor portal
      or posts RFPs via email distribution only)
    - City of Atlanta → atlantaga.gov (Akamai CDN bot protection, HTTP 403)
    - Hartsfield-Jackson → atl.com (Cloudflare, HTTP 403)
    - MARTA → itsmarta.com (all procurement paths return 404 or 500)
    - Grady Health → gradyhealth.org (404 on /procurement/)
    - DeKalb County Schools → dekalbschoolsga.org (404 on all procurement paths)
    - Cobb County Schools → cobbk12.org (404 on all procurement paths)
    - Gwinnett County Schools → gcpsk12.org (SSL cert issue + 302)
    - Fulton/DeKalb/Cobb/Gwinnett County governments → all 404 on procurement paths
    - Bonfire subdomains for most entities → DNS doesn't resolve

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

SECURITY_KEYWORDS = [
    "security", "surveillance", "camera", "video", "access control",
    "intrusion", "alarm", "monitoring", "cctv", "door", "lock",
    "visitor", "badge", "credential", "intercom", "public safety",
    "law enforcement", "emergency", "sensor", "detection",
    "safety", "weapon", "gun", "lpr", "license plate",
]

# ---------------------------------------------------------------------------
# Static portal mapping
# ---------------------------------------------------------------------------

PORTAL_REGISTRY = {
    "Atlanta Public Schools": {
        "entity_type": "k12_district",
        "portals": [
            {
                "name": "APS Main Site",
                "url": "https://www.atlantapublicschools.us/purchasing",
                "parser": "blackboard_cms",
                "notes": "Blackboard CMS — all procurement paths return 404 as of 2026-05",
            },
        ],
    },
    "Fulton County Schools": {
        "entity_type": "k12_district",
        "portals": [
            {
                "name": "Fulton Schools Bonfire",
                "url": "https://fultonschools.bonfirehub.com/portal",
                "parser": "bonfire_spa",
                "notes": "Bonfire JS SPA — returns 200 but data is client-rendered",
            },
        ],
    },
    "DeKalb County Schools": {
        "entity_type": "k12_district",
        "portals": [
            {
                "name": "DeKalb Schools Main Site",
                "url": "https://www.dekalbschoolsga.org/purchasing/",
                "parser": "generic_html",
                "notes": "All procurement paths return 404 as of 2026-05",
            },
        ],
    },
    "Cobb County Schools": {
        "entity_type": "k12_district",
        "portals": [
            {
                "name": "Cobb Schools Main Site",
                "url": "https://www.cobbk12.org/purchasing",
                "parser": "generic_html",
                "notes": "All procurement paths return 404 as of 2026-05",
            },
        ],
    },
    "Gwinnett County Schools": {
        "entity_type": "k12_district",
        "portals": [
            {
                "name": "Gwinnett Schools Main Site",
                "url": "https://www.gcpsk12.org/Page/34562",
                "parser": "generic_html",
                "notes": "SSL certificate issue + redirect as of 2026-05",
            },
        ],
    },
    "City of Atlanta": {
        "entity_type": "municipality",
        "portals": [
            {
                "name": "City of Atlanta Procurement",
                "url": "https://www.atlantaga.gov/government/departments/finance/office-of-procurement",
                "parser": "generic_html",
                "notes": "Akamai CDN bot protection — HTTP 403 Access Denied",
            },
        ],
    },
    "Fulton County": {
        "entity_type": "county",
        "portals": [
            {
                "name": "Fulton County Purchasing",
                "url": "https://www.fultoncountyga.gov/departments/purchasing",
                "parser": "generic_html",
                "notes": "Returns 404 on all procurement paths as of 2026-05",
            },
        ],
    },
    "DeKalb County": {
        "entity_type": "county",
        "portals": [
            {
                "name": "DeKalb County Solicitations",
                "url": "https://www.dekalbcountyga.gov/purchasing-and-contracting/solicitations",
                "parser": "generic_html",
                "notes": "Returns 404 on all procurement paths as of 2026-05",
            },
        ],
    },
    "Cobb County": {
        "entity_type": "county",
        "portals": [
            {
                "name": "Cobb County Purchasing",
                "url": "https://www.cobbcounty.org/purchasing",
                "parser": "generic_html",
                "notes": "Returns 404 (redirected to cobbcounty.gov) as of 2026-05",
            },
        ],
    },
    "Gwinnett County": {
        "entity_type": "county",
        "portals": [
            {
                "name": "Gwinnett County Purchasing",
                "url": "https://www.gwinnettcounty.com/departments/financialservices/purchasing",
                "parser": "generic_html",
                "notes": "Returns 404 as of 2026-05",
            },
        ],
    },
    "MARTA": {
        "entity_type": "transit",
        "portals": [
            {
                "name": "MARTA Procurement",
                "url": "https://www.itsmarta.com/doing-business.aspx",
                "parser": "generic_html",
                "notes": "All procurement paths return 404/500 as of 2026-05",
            },
        ],
    },
    "Hartsfield-Jackson": {
        "entity_type": "airport",
        "portals": [
            {
                "name": "ATL Airport Procurement",
                "url": "https://www.atl.com/business/procurement/",
                "parser": "generic_html",
                "notes": "Cloudflare bot protection — HTTP 403",
            },
        ],
    },
    "Grady Health System": {
        "entity_type": "healthcare",
        "portals": [
            {
                "name": "Grady Health Procurement",
                "url": "https://www.gradyhealth.org/procurement/",
                "parser": "generic_html",
                "notes": "Returns 404 as of 2026-05",
            },
        ],
    },
    "Georgia Institute of Technology": {
        "entity_type": "higher_ed",
        "portals": [
            {
                "name": "Georgia Tech Procurement",
                "url": "https://procurement.gatech.edu/",
                "parser": "drupal_gatech",
                "notes": "Drupal 10, server-rendered. Procurement info page but no solicitation listing.",
            },
            {
                "name": "Georgia Tech SciQuest",
                "url": "https://solutions.sciquest.com/apps/Router/Login?OrgName=GeorgiaTech",
                "parser": "sciquest_login",
                "notes": "SciQuest/Jaggaer eProcurement — requires login, not publicly accessible",
            },
        ],
    },
    "Georgia State University": {
        "entity_type": "higher_ed",
        "portals": [
            {
                "name": "GSU Procurement",
                "url": "https://finance.gsu.edu/procurement/",
                "parser": "generic_html",
                "notes": "Server-rendered procurement info page",
            },
        ],
    },
    "University System of Georgia": {
        "entity_type": "higher_ed",
        "portals": [
            {
                "name": "USG Procurement",
                "url": "https://www.usg.edu/procurement/",
                "parser": "generic_html",
                "notes": "Server-rendered, procurement policies and resources page",
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}


def attempt_fetch(url: str) -> dict:
    """Attempt to fetch a URL, return structured result."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        body = resp.text

        # Detect blocking patterns
        if resp.status_code == 403:
            if "Access Denied" in body or "akamai" in body.lower():
                return {"success": False, "error": "akamai_blocked", "status_code": 403,
                        "detail": "Akamai CDN bot protection — HTTP 403 Access Denied"}
            if "cloudflare" in body.lower() or "Just a moment" in body:
                return {"success": False, "error": "cloudflare_blocked", "status_code": 403,
                        "detail": "Cloudflare managed challenge — HTTP 403"}
            return {"success": False, "error": "http_403", "status_code": 403,
                    "detail": "HTTP 403 Forbidden"}

        if resp.status_code == 404:
            return {"success": False, "error": "not_found", "status_code": 404,
                    "detail": f"HTTP 404 — path does not exist at {resp.url}"}

        if resp.status_code >= 400:
            return {"success": False, "error": f"http_{resp.status_code}",
                    "status_code": resp.status_code,
                    "detail": f"HTTP {resp.status_code}"}

        # Check for soft 404 (200 but actually error page)
        if "404" in resp.url.lower() or "error" in resp.url.lower():
            return {"success": False, "error": "soft_404", "status_code": 200,
                    "detail": f"Redirected to error page: {resp.url}"}

        # Check for login walls
        if "login" in resp.url.lower() and "login" not in url.lower():
            return {"success": False, "error": "login_required", "status_code": 200,
                    "detail": f"Redirected to login: {resp.url}"}

        # Check for empty body
        if len(body.strip()) < 100:
            return {"success": False, "error": "empty_body", "status_code": 200,
                    "detail": "HTTP 200 but insufficient content"}

        # Detect JS SPA shells
        is_spa = ("ng-app" in body or "__NEXT_DATA__" in body
                  or "bf-top-bar" in body  # Bonfire
                  or ("react" in body.lower()[:3000] and "<div id=" in body[:2000]))

        return {
            "success": True,
            "status_code": resp.status_code,
            "effective_url": resp.url,
            "content_length": len(body),
            "is_spa": is_spa,
            "body": body,
        }
    except requests.exceptions.SSLError as e:
        return {"success": False, "error": "ssl_error", "status_code": 0,
                "detail": f"SSL certificate error: {str(e)[:100]}"}
    except requests.RequestException as e:
        return {"success": False, "error": "connection_error", "status_code": 0,
                "detail": str(e)[:200]}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_drupal_gatech(body: str) -> list[dict]:
    """Parse Georgia Tech procurement page (Drupal 10)."""
    items = []
    # Find links to PDFs and pages that might be solicitations
    for m in re.finditer(
        r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', body, re.S
    ):
        url, text = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        text_lower = text.lower()
        if any(kw in text_lower for kw in SECURITY_KEYWORDS):
            items.append({
                "title": text,
                "url": url if url.startswith("http") else f"https://procurement.gatech.edu{url}",
                "type": "link",
            })
    return items


def parse_generic_html(body: str, base_url: str) -> list[dict]:
    """Generic HTML parser — extract links matching security keywords."""
    items = []
    for m in re.finditer(
        r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', body, re.S
    ):
        url, text = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if not text or len(text) < 5:
            continue
        text_lower = text.lower()
        if any(kw in text_lower for kw in SECURITY_KEYWORDS + ["rfp", "bid", "solicitation"]):
            full_url = url if url.startswith("http") else f"{base_url.rstrip('/')}/{url.lstrip('/')}"
            items.append({"title": text, "url": full_url, "type": "link"})
    return items


def classify_items(items: list[dict]) -> dict:
    """Classify parsed items as RFPs vs general links."""
    active_rfps = []
    recent_awards = []
    other = []

    for item in items:
        title_lower = item.get("title", "").lower()
        if any(w in title_lower for w in ["rfp", "bid", "solicitation", "request for"]):
            active_rfps.append(item)
        elif any(w in title_lower for w in ["award", "contract", "vendor select"]):
            recent_awards.append(item)
        else:
            other.append(item)

    return {
        "active_rfps": active_rfps,
        "recent_awards": recent_awards,
        "other_security_links": other,
    }


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------

def resolve_entity(company_name: str) -> dict | None:
    """Find matching portal entry from static registry."""
    name_lower = company_name.lower().strip()
    for entity_name, config in PORTAL_REGISTRY.items():
        if entity_name.lower() in name_lower or name_lower in entity_name.lower():
            return {"entity_name": entity_name, **config}
    return None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def cache_path(company_slug: str) -> Path:
    return SOURCES_DIR / company_slug / "sled_procurement.json"


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
    try:
        ts = datetime.fromisoformat(retrieved_at)
    except ValueError:
        return None
    if datetime.now(timezone.utc) - ts > timedelta(days=CACHE_TTL_DAYS):
        return None
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

def fetch_sled_procurement_data(
    company_name: str, *, force_refresh: bool = False
) -> dict:
    """
    Fetch procurement data for a Georgia SLED entity.

    Resolves company_name to known procurement portal(s), attempts to scrape
    active RFPs and recent awards filtered for security keywords.
    """
    company_slug = slugify(company_name)

    if not force_refresh:
        cached = read_cache(company_slug)
        if cached is not None:
            return cached

    entity = resolve_entity(company_name)

    if entity is None:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "sled_procurement",
            "status": "insufficient_data",
            "reason": (
                f"No portal mapping for '{company_name}'. "
                f"Known entities: {', '.join(PORTAL_REGISTRY.keys())}"
            ),
            "company_name": company_name,
            "active_rfps": [],
            "recent_awards": [],
            "blocked_endpoints": [],
        }
        write_cache(company_slug, result)
        return result

    print(f"  [sled_procurement] resolving {entity['entity_name']}...", file=sys.stderr)

    active_rfps = []
    recent_awards = []
    blocked_endpoints = []
    accessible_endpoints = []

    for portal in entity["portals"]:
        url = portal["url"]
        name = portal["name"]
        parser_type = portal["parser"]

        print(f"  [sled_procurement] probing {name} ({url})...", file=sys.stderr)
        fetch_result = attempt_fetch(url)

        if not fetch_result["success"]:
            error_detail = fetch_result.get("detail", fetch_result.get("error", "unknown"))
            print(f"  [sled_procurement] {name}: blocked — {error_detail}", file=sys.stderr)
            blocked_endpoints.append({
                "name": name,
                "url": url,
                "error": fetch_result["error"],
                "detail": error_detail,
                "notes": portal.get("notes", ""),
            })
            continue

        # Successful fetch
        body = fetch_result["body"]
        is_spa = fetch_result.get("is_spa", False)

        print(
            f"  [sled_procurement] {name}: accessible "
            f"({fetch_result['content_length']} bytes"
            f"{', SPA' if is_spa else ''})",
            file=sys.stderr,
        )

        if is_spa:
            # SPA content is client-rendered — can't parse server HTML
            blocked_endpoints.append({
                "name": name,
                "url": url,
                "error": "js_spa",
                "detail": "Content is JavaScript SPA — data rendered client-side, not in initial HTML",
                "notes": portal.get("notes", ""),
                "accessible": True,
                "requires_browser": True,
            })
            accessible_endpoints.append({
                "name": name,
                "url": fetch_result.get("effective_url", url),
                "content_length": fetch_result["content_length"],
                "is_spa": True,
                "items_found": 0,
            })
            continue

        # Parse based on type
        if parser_type == "drupal_gatech":
            items = parse_drupal_gatech(body)
        elif parser_type == "sciquest_login":
            blocked_endpoints.append({
                "name": name,
                "url": url,
                "error": "login_required",
                "detail": "SciQuest/Jaggaer eProcurement requires login credentials",
                "notes": portal.get("notes", ""),
            })
            continue
        else:
            items = parse_generic_html(body, url)

        classified = classify_items(items)
        active_rfps.extend(classified["active_rfps"])
        recent_awards.extend(classified["recent_awards"])

        accessible_endpoints.append({
            "name": name,
            "url": fetch_result.get("effective_url", url),
            "content_length": fetch_result["content_length"],
            "is_spa": False,
            "items_found": len(items),
        })

    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "sled_procurement",
        "source_url": entity["portals"][0]["url"] if entity["portals"] else None,
        "company_name": company_name,
        "entity_name": entity["entity_name"],
        "entity_type": entity["entity_type"],
        "active_rfps": active_rfps,
        "recent_awards": recent_awards,
        "blocked_endpoints": blocked_endpoints,
        "accessible_endpoints": accessible_endpoints,
        "summary": {
            "total_portals_checked": len(entity["portals"]),
            "accessible_count": len(accessible_endpoints),
            "blocked_count": len(blocked_endpoints),
            "active_rfps_found": len(active_rfps),
            "recent_awards_found": len(recent_awards),
            "source_quality": "primary" if active_rfps or recent_awards else "insufficient_data",
            "confidence": "high" if active_rfps else "medium" if accessible_endpoints else "none",
        },
        "status": (
            "ok" if active_rfps or recent_awards
            else "accessible_no_rfps" if accessible_endpoints
            else "insufficient_data"
        ),
        "reason": (
            None if active_rfps or recent_awards
            else "Portal accessible but no security-relevant RFPs found in HTML" if accessible_endpoints
            else (
                f"All {len(blocked_endpoints)} portal(s) blocked: "
                + "; ".join(ep["error"] for ep in blocked_endpoints)
            )
        ),
    }

    write_cache(company_slug, result)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python sled_procurement.py <company_name> [--force]", file=sys.stderr)
        print(f"Known entities: {', '.join(PORTAL_REGISTRY.keys())}", file=sys.stderr)
        sys.exit(1)

    company = sys.argv[1]
    force = "--force" in sys.argv

    result = fetch_sled_procurement_data(company, force_refresh=force)

    summary = result.get("summary", {})
    status = result.get("status", "unknown")
    print(
        f"\n  SLED Procurement: {company}\n"
        f"  Status: {status}\n"
        f"  Portals checked: {summary.get('total_portals_checked', 0)}\n"
        f"  Accessible: {summary.get('accessible_count', 0)}\n"
        f"  Blocked: {summary.get('blocked_count', 0)}\n"
        f"  Active RFPs: {summary.get('active_rfps_found', 0)}\n"
        f"  Recent Awards: {summary.get('recent_awards_found', 0)}",
        file=sys.stderr,
    )

    if result.get("blocked_endpoints"):
        print("\n  Blocked endpoints:", file=sys.stderr)
        for ep in result["blocked_endpoints"]:
            print(f"    {ep['name']}: {ep['error']} — {ep['detail']}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
