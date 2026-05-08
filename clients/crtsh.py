"""
crt.sh Client — SSL certificate transparency log analysis for tech footprint fingerprinting.

Language choice: Python (shared pattern with clients/sec.py and clients/indeed.py)

Data source: crt.sh free JSON API (https://crt.sh/?q=%25.domain.com&output=json)
  - Certificate transparency logs indexed by Sectigo's crt.sh service
  - Returns all SSL certificates ever issued for a domain and subdomains
  - No auth required, no API key, CORS-open

What it gives a Verkada SE:
  - Subdomain enumeration → SaaS/vendor fingerprinting
  - Security vendor detection (Genetec, Avigilon, Hikvision, Lenel subdomains)
  - Infrastructure patterns (VPN, badge, visitor, alarm subdomains)
  - Cloud vs on-prem posture signals
  - Scale indicator (subdomain count correlates with org complexity)

Fragility points:
  1. crt.sh is slow (5-30s per query) and returns 502/503 under load. Retry with backoff.
  2. Most enterprises do NOT name subdomains after vendors. Direct vendor hits are rare but
     high-signal when found. The real value is infrastructure pattern detection.
  3. Expired certs are included — filter by not_after to distinguish current vs historical infra.
  4. name_value field contains newline-separated SANs AND email addresses — must filter.

Domain resolution: Company name → primary domain via simple heuristic + validation.
Extraction: Claude Haiku per CLAUDE.md model assignment.
Cache TTL: 90 days per CLAUDE.md (same as SEC filings — cert data is slow-moving).
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
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

CRTSH_URL = "https://crt.sh/"
CRTSH_TIMEOUT = 45  # crt.sh can be very slow

# Cache TTL: 90 days (cert data is slow-moving)
CACHE_TTL_DAYS = 90

# Retry config for crt.sh flakiness
MAX_RETRIES = 3
RETRY_BACKOFF = [5, 15, 30]  # seconds

# ---------------------------------------------------------------------------
# Domain resolution
# ---------------------------------------------------------------------------

# Common company name → domain mappings for known targets
KNOWN_DOMAINS = {
    "target": "target.com",
    "target corporation": "target.com",
    "apple": "apple.com",
    "microsoft": "microsoft.com",
    "walmart": "walmart.com",
    "amazon": "amazon.com",
    "google": "google.com",
    "meta": "meta.com",
}


def resolve_domain(company_name: str) -> str:
    """
    Resolve a company name to its primary domain.
    Uses known mappings first, then falls back to {slug}.com with DNS validation.
    """
    normalized = company_name.strip().lower()

    # Check known mappings
    if normalized in KNOWN_DOMAINS:
        return KNOWN_DOMAINS[normalized]

    # Strip common suffixes and try {name}.com
    for suffix in [" corporation", " incorporated", " inc", " corp", " co", " ltd", " llc", " group"]:
        normalized = normalized.replace(suffix, "")
    normalized = normalized.strip()

    slug = re.sub(r"[^a-z0-9]+", "", normalized)
    candidate = f"{slug}.com"

    # Quick DNS check via a lightweight HTTP HEAD
    try:
        resp = requests.head(f"https://{candidate}", timeout=5, allow_redirects=True)
        if resp.status_code < 500:
            return candidate
    except requests.RequestException:
        pass

    # Try with hyphenated version
    slug_hyphen = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    candidate_hyphen = f"{slug_hyphen}.com"
    if candidate_hyphen != candidate:
        try:
            resp = requests.head(f"https://{candidate_hyphen}", timeout=5, allow_redirects=True)
            if resp.status_code < 500:
                return candidate_hyphen
        except requests.RequestException:
            pass

    # Return best guess even without validation
    return candidate


# ---------------------------------------------------------------------------
# Persona loading
# ---------------------------------------------------------------------------

def _load_persona() -> dict:
    """Load the full persona YAML."""
    if not PERSONA_PATH.exists():
        return {}
    try:
        return yaml.safe_load(PERSONA_PATH.read_text()) or {}
    except Exception:
        return {}


def _load_persona_context(persona: dict) -> str:
    """Format Verkada context string for Haiku prompts."""
    if not persona:
        return ""

    product = persona.get("product", {})
    displace = persona.get("displacement_targets", [])

    lines = [f"Verkada product lines: {', '.join(product.get('lines', []))}"]
    lines.append(f"Unified under: {product.get('unified_under', 'Command platform')}")
    lines.append(f"Positioning: {product.get('positioning', '')}")

    diffs = product.get("key_differentiators", [])
    if diffs:
        lines.append("Key differentiators:")
        for d in diffs:
            if isinstance(d, dict):
                for k, v in d.items():
                    lines.append(f"  - {k}: {v}")
            else:
                lines.append(f"  - {d}")

    if displace:
        lines.append("Displacement targets:")
        for d in displace:
            if isinstance(d, dict):
                vendor = d.get("vendor", "")
                pain = ", ".join(d.get("common_pain", []))
                lines.append(f"  - {vendor}: {pain}")

    return "\n".join(lines)


def _extract_displacement_patterns(persona: dict) -> dict[str, list[str]]:
    """
    Build vendor fingerprint patterns from displacement_targets in persona.
    Returns {vendor_name: [subdomain_pattern1, pattern2, ...]}.
    """
    targets = persona.get("displacement_targets", [])
    patterns = {}

    # Vendor-specific subdomain patterns (vendor name + product names)
    # Use regex-style matching: patterns are checked as whole-word or dot-delimited segments
    # to avoid false positives (e.g., "s2" matching "sites2", "acc" matching "directaccess")
    vendor_patterns = {
        "Avigilon": ["avigilon", "acc7"],
        "Genetec": ["genetec", "synergis", "omnicast", "security-center"],
        "Milestone": ["milestone", "xprotect"],
        "Lenel": ["lenel", "onguard"],
        "Hikvision": ["hikvision", "hikcentral"],
        "Dahua": ["dahua"],
        "March_Networks": ["marchnetworks", "march-networks"],
        "Brivo": ["brivo", "openpath"],
    }

    for target in targets:
        if not isinstance(target, dict):
            continue
        vendor = target.get("vendor", "")
        if vendor in vendor_patterns:
            patterns[vendor] = vendor_patterns[vendor]

    return patterns


# ---------------------------------------------------------------------------
# Infrastructure pattern detection (deterministic, no LLM)
# ---------------------------------------------------------------------------

# Subdomain patterns that indicate physical security or IT infrastructure
INFRA_PATTERNS = {
    "physical_security": [
        "camera", "cam", "cctv", "nvr", "dvr", "vms", "surveillance",
        "security", "guard", "patrol",
    ],
    "access_control": [
        "badge", "access-control", "accesscontrol", "door", "entry",
        "visitor", "lobby", "kiosk", "turnstile",
    ],
    "alarm_sensor": [
        "alarm", "intrusion", "sensor", "monitoring", "alert",
    ],
    "network_infra": [
        "vpn", "firewall", "fw", "proxy", "waf", "ids", "ips",
        "siem", "soc", "noc",
    ],
    "cloud_saas": [
        "aws", "azure", "gcp", "cloud", "saas", "cdn", "api",
        "oauth", "sso", "okta", "auth0",
    ],
    "facilities": [
        "facility", "facilities", "building", "hvac", "bms",
        "energy", "parking", "elevator",
    ],
    "verkada_existing": [
        "verkada", "command",
    ],
}


def classify_subdomains(subdomains: set[str]) -> dict:
    """
    Classify subdomains into infrastructure categories and detect vendor fingerprints.
    Returns deterministic matches — no LLM needed.
    """
    results = {
        "vendor_hits": {},       # vendor -> [matching subdomains]
        "infra_categories": {},  # category -> [matching subdomains]
        "total_unique": len(subdomains),
    }

    persona = _load_persona()
    vendor_patterns = _extract_displacement_patterns(persona)

    for subdomain in sorted(subdomains):
        sub_lower = subdomain.lower()

        # Check vendor patterns using dot-delimited segment matching
        # Split "stg-genetec.corp.target.com" into ["stg-genetec", "corp", "target", "com"]
        segments = sub_lower.split(".")
        segment_text = " ".join(segments)  # for multi-char pattern matching

        for vendor, patterns in vendor_patterns.items():
            for pattern in patterns:
                # Match if pattern appears as a full segment or hyphen-delimited part
                if any(pattern == seg or pattern in seg.split("-") for seg in segments):
                    if vendor not in results["vendor_hits"]:
                        results["vendor_hits"][vendor] = []
                    results["vendor_hits"][vendor].append({
                        "subdomain": subdomain,
                        "matched_pattern": pattern,
                        "confidence": "high",
                    })
                    break

        # Check infrastructure patterns
        for category, patterns in INFRA_PATTERNS.items():
            for pattern in patterns:
                if pattern in sub_lower:
                    if category not in results["infra_categories"]:
                        results["infra_categories"][category] = []
                    results["infra_categories"][category].append(subdomain)
                    break

    return results


# ---------------------------------------------------------------------------
# crt.sh API
# ---------------------------------------------------------------------------

def fetch_crtsh(domain: str) -> list[dict]:
    """
    Fetch certificate transparency data from crt.sh.
    Returns raw JSON array of certificate entries.
    """
    params = {
        "q": f"%.{domain}",
        "output": "json",
    }

    for attempt in range(MAX_RETRIES):
        try:
            print(f"  [crt.sh] querying %.{domain} (attempt {attempt + 1})…", file=sys.stderr)
            resp = requests.get(CRTSH_URL, params=params, timeout=CRTSH_TIMEOUT)

            if resp.status_code == 503 or resp.status_code == 502:
                wait = RETRY_BACKOFF[attempt] if attempt < len(RETRY_BACKOFF) else 30
                print(f"  [crt.sh] {resp.status_code}, retrying in {wait}s…", file=sys.stderr)
                time.sleep(wait)
                continue

            resp.raise_for_status()

            # crt.sh sometimes returns empty body on success
            if not resp.text.strip():
                return []

            return resp.json()

        except requests.Timeout:
            wait = RETRY_BACKOFF[attempt] if attempt < len(RETRY_BACKOFF) else 30
            print(f"  [crt.sh] timeout, retrying in {wait}s…", file=sys.stderr)
            time.sleep(wait)
            continue
        except requests.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                print(f"  [crt.sh] error: {e}, retrying in {wait}s…", file=sys.stderr)
                time.sleep(wait)
                continue
            raise

    print("  [crt.sh] all retries exhausted", file=sys.stderr)
    return []


def extract_subdomains(certs: list[dict], domain: str) -> set[str]:
    """
    Extract unique subdomains from certificate name_value fields.
    Filters out email addresses and non-matching domains.
    """
    subdomains = set()
    domain_lower = domain.lower()

    for cert in certs:
        # name_value contains newline-separated SANs + emails
        name_value = cert.get("name_value", "")
        for line in name_value.split("\n"):
            line = line.strip().lower()
            # Skip emails
            if "@" in line:
                continue
            # Skip wildcards (*.domain.com → not a real subdomain)
            if line.startswith("*."):
                line = line[2:]
            # Must belong to the target domain
            if line.endswith(f".{domain_lower}") or line == domain_lower:
                subdomains.add(line)

        # Also check common_name
        cn = (cert.get("common_name") or "").strip().lower()
        if cn.startswith("*."):
            cn = cn[2:]
        if cn and (cn.endswith(f".{domain_lower}") or cn == domain_lower) and "@" not in cn:
            subdomains.add(cn)

    return subdomains


def partition_certs_by_validity(certs: list[dict]) -> dict:
    """Split certs into current (not expired) and expired based on not_after."""
    now = datetime.now(timezone.utc)
    current = 0
    expired = 0
    for cert in certs:
        not_after = cert.get("not_after", "")
        if not_after:
            try:
                expiry = datetime.fromisoformat(not_after).replace(tzinfo=timezone.utc)
                if expiry > now:
                    current += 1
                else:
                    expired += 1
            except ValueError:
                expired += 1
        else:
            expired += 1
    return {"current_certs": current, "expired_certs": expired}


# ---------------------------------------------------------------------------
# LLM extraction via Haiku
# ---------------------------------------------------------------------------

def analyze_with_haiku(
    subdomains: set[str],
    classification: dict,
    company_name: str,
    domain: str,
    persona: dict,
) -> dict:
    """
    Light Haiku pass to categorize subdomains and surface displacement/trigger signals.
    """
    if not subdomains or len(subdomains) < 5:
        return {
            "status": "insufficient_data",
            "reason": f"Only {len(subdomains)} subdomains found — too few for meaningful infrastructure analysis",
        }

    client = anthropic.Anthropic()
    persona_ctx = _load_persona_context(persona)

    # Build a compact subdomain list for Haiku (cap at 300 to manage context)
    sorted_subs = sorted(subdomains)
    sub_list = sorted_subs[:300]
    truncated = len(sorted_subs) > 300

    system_prompt = (
        "You are a technical infrastructure analyst examining SSL certificate subdomain data "
        "for a Verkada Solutions Engineer's pre-sales research tool.\n\n"
        "## Verkada Context (use this to judge relevance)\n"
        f"{persona_ctx}\n\n"
        "## Your Task\n"
        "Analyze the subdomain list to infer the company's technology footprint, with focus on:\n"
        "1. Physical security infrastructure (cameras, NVR, VMS, access control)\n"
        "2. Displacement targets — any subdomain suggesting Avigilon, Genetec, Milestone, Lenel, "
        "Hikvision, Dahua, March Networks, or Brivo infrastructure\n"
        "3. Cloud vs on-prem posture (SaaS tools vs self-hosted infrastructure)\n"
        "4. Scale signals (number of subdomains, geographic patterns, facility-related names)\n"
        "5. IT infrastructure patterns relevant to security system deployment\n\n"
        "## Output Schema\n"
        "Output ONLY valid JSON with no markdown formatting. Use this exact schema:\n"
        '{"findings": [{'
        '"signal": "one-sentence description — MUST reference specific subdomains from the data", '
        '"evidence_subdomains": ["sub1.domain.com", "sub2.domain.com"], '
        '"category": "one of: displacement_target|physical_security|access_control|cloud_posture|'
        'scale_indicator|IT_infrastructure|verkada_existing_customer|other", '
        '"confidence": "one of: high|medium|inference — '
        'high = subdomain directly names a vendor/product, '
        'medium = subdomain pattern strongly implies a technology, '
        'inference = interpretation requiring additional context", '
        '"verkada_relevant": true/false'
        '}], '
        '"tech_posture_summary": "2-3 sentence summary of overall infrastructure posture as it relates to physical security — reference specific subdomains"}\n\n'
        "## Anti-Genericness Rules (MANDATORY)\n"
        "- Every finding must cite SPECIFIC subdomains from the list. No vague claims.\n"
        "- If a finding could appear in any company's report, drop it.\n"
        "- Do NOT use hedging words (likely, potentially, may) unless paired with confidence: inference.\n"
        "- Subdomain analysis is inherently inferential — be honest about confidence levels.\n"
        "- If the subdomains are too generic to extract meaningful signals, return:\n"
        '  {"findings": [], "tech_posture_summary": "insufficient_data", '
        '"status": "insufficient_data", "reason": "Subdomains are primarily generic infrastructure with no security-relevant patterns"}\n'
        "- Extract at most 10 findings. Quality over quantity.\n"
    )

    user_msg = (
        f"Company: {company_name}\n"
        f"Domain: {domain}\n"
        f"Total unique subdomains: {len(subdomains)}"
        f"{' (showing first 300)' if truncated else ''}\n"
        f"Source: crt.sh certificate transparency logs\n\n"
        f"Subdomains:\n{chr(10).join(sub_list)}"
    )

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        return {"status": "extraction_error", "reason": f"Haiku returned invalid JSON: {e}", "raw_snippet": raw[:500]}
    except anthropic.APIError as e:
        return {"status": "extraction_error", "reason": f"Anthropic API error: {e}"}
    except TypeError as e:
        if "api_key" in str(e) or "auth_token" in str(e):
            return {"status": "extraction_error", "reason": "ANTHROPIC_API_KEY not set. Set the env var or run via Claude Code."}
        raise


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def cache_path(company_slug: str) -> Path:
    return SOURCES_DIR / company_slug / "ssl.json"


def read_cache(company_slug: str) -> dict | None:
    """Read cached ssl.json if it exists and is within 90-day TTL."""
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

    retrieved_dt = datetime.fromisoformat(retrieved_at)
    age_days = (datetime.now(timezone.utc) - retrieved_dt).days
    if age_days > CACHE_TTL_DAYS:
        print(f"  [cache] ssl.json is {age_days}d old (TTL={CACHE_TTL_DAYS}d), refetching", file=sys.stderr)
        return None

    print(f"  [cache] ssl.json is {age_days}d old, within TTL", file=sys.stderr)
    return data


def write_cache(company_slug: str, data: dict) -> Path:
    """Write structured data to sources/{company}/ssl.json."""
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


def fetch_crtsh_data(company_name: str, *, domain: str = "", force_refresh: bool = False) -> dict:
    """
    Full pipeline: resolve domain → fetch crt.sh → extract subdomains → classify → Haiku analysis → cache.

    Args:
        company_name: Company name for slug and Haiku context.
        domain: Override domain (skip resolution). If empty, resolved from company name.
        force_refresh: Ignore cache.

    Returns the structured JSON dict (also written to sources/{company}/ssl.json).
    """
    company_slug = slugify(company_name)

    # Check cache first
    if not force_refresh:
        cached = read_cache(company_slug)
        if cached is not None:
            return cached

    # Resolve domain
    if not domain:
        domain = resolve_domain(company_name)
    print(f"  [domain] using {domain}", file=sys.stderr)

    persona = _load_persona()

    # Step 1: Fetch from crt.sh
    raw_certs = fetch_crtsh(domain)

    if not raw_certs:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "crt.sh",
            "source_url": f"https://crt.sh/?q=%25.{domain}&output=json",
            "status": "insufficient_data",
            "reason": f"No certificate transparency data found for {domain}. The domain may not use publicly-issued SSL certificates.",
            "company": {"name": company_name, "domain": domain},
            "subdomains": [],
            "classification": {},
            "haiku_analysis": {},
        }
        write_cache(company_slug, result)
        return result

    # Step 2: Extract and deduplicate subdomains
    subdomains = extract_subdomains(raw_certs, domain)
    cert_stats = partition_certs_by_validity(raw_certs)
    print(f"  [crt.sh] {len(raw_certs)} certs → {len(subdomains)} unique subdomains", file=sys.stderr)

    # Step 3: Deterministic classification (no LLM)
    classification = classify_subdomains(subdomains)

    vendor_hit_count = sum(len(v) for v in classification["vendor_hits"].values())
    infra_hit_count = sum(len(v) for v in classification["infra_categories"].values())
    print(
        f"  [classify] {vendor_hit_count} vendor hit(s), "
        f"{infra_hit_count} infra pattern(s) across {len(classification['infra_categories'])} categories",
        file=sys.stderr,
    )

    # Step 4: Haiku analysis
    haiku_analysis = analyze_with_haiku(subdomains, classification, company_name, domain, persona)

    # Step 5: Assemble and cache
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "crt.sh",
        "source_url": f"https://crt.sh/?q=%25.{domain}&output=json",
        "company": {
            "name": company_name,
            "domain": domain,
        },
        "certificate_stats": {
            "total_certs": len(raw_certs),
            **cert_stats,
        },
        "subdomains": {
            "total_unique": len(subdomains),
            "list": sorted(subdomains),
        },
        "classification": classification,
        "haiku_analysis": haiku_analysis,
        "summary": {
            "vendor_hits": {v: len(hits) for v, hits in classification["vendor_hits"].items()},
            "infra_categories_detected": list(classification["infra_categories"].keys()),
            "verkada_relevant_findings": (
                sum(1 for f in haiku_analysis.get("findings", []) if f.get("verkada_relevant"))
                if isinstance(haiku_analysis.get("findings"), list) else 0
            ),
        },
    }

    write_cache(company_slug, result)
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python crtsh.py <company_name> [--domain example.com] [--force]", file=sys.stderr)
        print("  e.g.: python crtsh.py 'Target Corporation'", file=sys.stderr)
        print("        python crtsh.py 'Apple' --domain apple.com", file=sys.stderr)
        sys.exit(1)

    company_name = sys.argv[1]
    force = "--force" in sys.argv

    # Parse --domain flag
    domain = ""
    if "--domain" in sys.argv:
        idx = sys.argv.index("--domain")
        if idx + 1 < len(sys.argv):
            domain = sys.argv[idx + 1]

    try:
        result = fetch_crtsh_data(company_name, domain=domain, force_refresh=force)

        if result.get("status") == "insufficient_data":
            print(f"\n  {result['reason']}", file=sys.stderr)
        else:
            summary = result["summary"]
            subs = result["subdomains"]
            print(
                f"\n  Done: {result['company']['name']} ({result['company']['domain']})\n"
                f"  Certificates: {result['certificate_stats']['total_certs']} "
                f"({result['certificate_stats']['current_certs']} current, "
                f"{result['certificate_stats']['expired_certs']} expired)\n"
                f"  Unique subdomains: {subs['total_unique']}\n"
                f"  Vendor hits: {json.dumps(summary['vendor_hits']) if summary['vendor_hits'] else 'none'}\n"
                f"  Infra categories: {', '.join(summary['infra_categories_detected']) or 'none'}\n"
                f"  Verkada-relevant findings: {summary['verkada_relevant_findings']}\n"
                f"  Cached to: sources/{slugify(company_name)}/ssl.json",
                file=sys.stderr,
            )

        print(json.dumps(result, indent=2, default=str))

    except requests.HTTPError as e:
        print(f"ERROR: crt.sh HTTP error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
