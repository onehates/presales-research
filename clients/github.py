"""
GitHub Client — Org-level engineering footprint analysis via GitHub REST API.

Language choice: Python (shared pattern with clients/sec.py, indeed.py, crtsh.py)

Data source: GitHub REST API v3 (https://api.github.com)
  - Unauthenticated: 60 requests/hour (sufficient for one company per run)
  - Authenticated (GITHUB_TOKEN env var): 5,000 requests/hour
  - No signup required for unauthenticated access

What it gives a Verkada SE:
  - Engineering scale (repo count, contributor activity, org size)
  - Tech stack distribution (language breakdown across repos)
  - Cloud-native indicators (Kubernetes, Terraform, Docker repos)
  - Security posture signals (security-related repos, vulnerability scanning tools)
  - Open-source culture (signals IT sophistication and vendor evaluation rigor)

Fragility points:
  1. Many companies have NO public GitHub presence. Manufacturing, healthcare, K-12, and
     government orgs rarely publish code. insufficient_data is the expected output for most
     Verkada ICP verticals. This source is strongest for tech/SaaS companies.
  2. Org name mismatch: company "Target Corporation" may be "target" or "targetretail" or
     "TargetCorp" on GitHub. We try multiple slug variants and validate via API.
  3. Acquired companies: repos may live under parent org (e.g., Avigilon repos under
     Motorola Solutions). We report what we find, not what we assume.
  4. Public repos only. Most enterprise code is private. The public footprint is a lower
     bound on engineering activity.
  5. Unauthenticated rate limit (60/hr) means we can't enumerate large orgs fully.
     We fetch top 20 repos sorted by most recently updated. Set GITHUB_TOKEN for richer data.

Extraction: Claude Haiku per CLAUDE.md model assignment.
Cache TTL: 30 days per CLAUDE.md caching rules for GitHub data.
"""

import json
import os
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

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

CACHE_TTL_DAYS = 30

# Rate-limit: minimum seconds between GitHub API requests
REQUEST_INTERVAL = 0.5 if GITHUB_TOKEN else 1.5  # more conservative unauthenticated

MAX_REPOS = 20

_last_request_time = 0.0


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _github_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _github_get(url: str, *, params: dict = None, timeout: int = 15) -> requests.Response:
    """Rate-limited GitHub API GET."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)

    resp = requests.get(url, headers=_github_headers(), params=params, timeout=timeout)
    _last_request_time = time.time()

    if resp.status_code == 403:
        # Rate limit exceeded
        remaining = resp.headers.get("X-RateLimit-Remaining", "?")
        reset_ts = resp.headers.get("X-RateLimit-Reset", "")
        if remaining == "0" and reset_ts:
            reset_dt = datetime.fromtimestamp(int(reset_ts), tz=timezone.utc)
            wait = max(0, (reset_dt - datetime.now(timezone.utc)).total_seconds()) + 1
            if wait < 300:  # only wait if under 5 minutes
                print(f"  [github] rate limited, waiting {wait:.0f}s until reset…", file=sys.stderr)
                time.sleep(wait)
                return _github_get(url, params=params, timeout=timeout)
            else:
                print(f"  [github] rate limited, reset in {wait:.0f}s — too long to wait", file=sys.stderr)

    return resp


# ---------------------------------------------------------------------------
# Org resolution
# ---------------------------------------------------------------------------

def resolve_github_org(company_name: str) -> dict | None:
    """
    Try to resolve a company name to a GitHub organization.
    Tries multiple slug variants and validates each via the API.
    Returns org metadata dict or None.
    """
    name = company_name.strip().lower()

    # Strip common corporate suffixes
    for suffix in [" corporation", " incorporated", " inc.", " inc", " corp.", " corp",
                   " co.", " co", " ltd.", " ltd", " llc", " group", " labs", " technologies"]:
        name = name.replace(suffix, "")
    name = name.strip()

    # Generate candidate slugs
    candidates = []
    # As-is, lowered, no spaces
    slug = re.sub(r"[^a-z0-9]+", "", name)
    candidates.append(slug)
    # Hyphenated
    slug_hyphen = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    if slug_hyphen != slug:
        candidates.append(slug_hyphen)
    # Original casing (some orgs like "HashiCorp" use mixed case)
    original_stripped = re.sub(r"[^a-zA-Z0-9]+", "", company_name.strip())
    if original_stripped.lower() not in [c.lower() for c in candidates]:
        candidates.append(original_stripped)
    # Common abbreviations
    words = name.split()
    if len(words) > 1:
        # First letters (e.g., "Johnson Controls" -> "jc")
        initials = "".join(w[0] for w in words if w)
        candidates.append(initials)
        # First word only
        candidates.append(words[0])

    # Deduplicate while preserving order
    seen = set()
    unique_candidates = []
    for c in candidates:
        cl = c.lower()
        if cl and cl not in seen:
            seen.add(cl)
            unique_candidates.append(c)

    print(f"  [github] trying org slugs: {unique_candidates}", file=sys.stderr)

    for candidate in unique_candidates:
        resp = _github_get(f"{GITHUB_API}/orgs/{candidate}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"  [github] found org: {data.get('login')} ({data.get('name', 'no name')})", file=sys.stderr)
            return data
        elif resp.status_code == 403:
            # Rate limited, stop trying
            print("  [github] rate limited during org resolution", file=sys.stderr)
            return None

    print(f"  [github] no org found for '{company_name}'", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Persona loading
# ---------------------------------------------------------------------------

def _load_persona() -> dict:
    if not PERSONA_PATH.exists():
        return {}
    try:
        return yaml.safe_load(PERSONA_PATH.read_text()) or {}
    except Exception:
        return {}


def _load_persona_context(persona: dict) -> str:
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
        vendors = [d.get("vendor", "") for d in displace if isinstance(d, dict)]
        lines.append(f"Displacement targets (incumbent vendors): {', '.join(vendors)}")

    return "\n".join(lines)


def _extract_trigger_keywords_for_github(persona: dict) -> dict[str, list[str]]:
    """Extract keywords from triggers that include 'github' in source_hints."""
    triggers = persona.get("triggers", [])
    result = {}
    for trigger in triggers:
        if not isinstance(trigger, dict):
            continue
        hints = trigger.get("detect_signals", {}).get("source_hints", [])
        if "github" not in hints:
            continue
        keywords = trigger.get("detect_signals", {}).get("keywords", [])
        if keywords:
            result[trigger["id"]] = [k.lower() for k in keywords]
    return result


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_org_repos(org_login: str) -> list[dict]:
    """Fetch top repos sorted by most recently updated."""
    resp = _github_get(
        f"{GITHUB_API}/orgs/{org_login}/repos",
        params={"sort": "updated", "direction": "desc", "per_page": str(MAX_REPOS), "type": "public"},
    )
    if resp.status_code != 200:
        print(f"  [github] repos fetch failed: {resp.status_code}", file=sys.stderr)
        return []
    return resp.json()


def compute_language_distribution(repos: list[dict]) -> dict[str, int]:
    """Aggregate primary language counts across repos."""
    dist = {}
    for repo in repos:
        lang = repo.get("language")
        if lang:
            dist[lang] = dist.get(lang, 0) + 1
    return dict(sorted(dist.items(), key=lambda x: -x[1]))


def detect_infra_signals(repos: list[dict]) -> dict:
    """
    Deterministic scan of repo names/descriptions/topics for infrastructure signals.
    No LLM needed.
    """
    signals = {
        "cloud_native": [],     # K8s, Terraform, Docker, cloud SDKs
        "security_repos": [],   # security-related repos
        "infrastructure": [],   # CI/CD, monitoring, config management
        "iot_embedded": [],     # IoT, embedded, firmware
    }

    cloud_patterns = ["kubernetes", "k8s", "terraform", "docker", "helm", "aws", "azure", "gcp",
                      "cloud", "serverless", "lambda", "ecs", "eks", "aks"]
    security_patterns = ["security", "vuln", "cve", "auth", "oauth", "crypto", "tls", "ssl",
                         "firewall", "ids", "siem", "pentest", "compliance", "audit"]
    infra_patterns = ["ci", "cd", "pipeline", "deploy", "ansible", "puppet", "chef",
                      "monitoring", "prometheus", "grafana", "datadog", "jenkins", "github-actions"]
    iot_patterns = ["iot", "embedded", "firmware", "mqtt", "edge", "sensor", "device",
                    "raspberry", "arduino", "camera"]

    for repo in repos:
        name = (repo.get("name") or "").lower()
        desc = (repo.get("description") or "").lower()
        topics = [t.lower() for t in (repo.get("topics") or [])]
        searchable = f"{name} {desc} {' '.join(topics)}"

        repo_ref = {
            "name": repo.get("full_name", ""),
            "url": repo.get("html_url", ""),
            "stars": repo.get("stargazers_count", 0),
        }

        for pattern in cloud_patterns:
            if pattern in searchable:
                signals["cloud_native"].append({**repo_ref, "matched_pattern": pattern})
                break

        for pattern in security_patterns:
            if pattern in searchable:
                signals["security_repos"].append({**repo_ref, "matched_pattern": pattern})
                break

        for pattern in infra_patterns:
            if pattern in searchable:
                signals["infrastructure"].append({**repo_ref, "matched_pattern": pattern})
                break

        for pattern in iot_patterns:
            if pattern in searchable:
                signals["iot_embedded"].append({**repo_ref, "matched_pattern": pattern})
                break

    return signals


def match_trigger_keywords(repos: list[dict], persona: dict) -> dict:
    """Match repo names/descriptions against trigger keywords from verkada-se.yml."""
    trigger_keywords = _extract_trigger_keywords_for_github(persona)
    matches = {}

    for repo in repos:
        name = (repo.get("name") or "").lower()
        desc = (repo.get("description") or "").lower()
        searchable = f"{name} {desc}"

        for trigger_id, keywords in trigger_keywords.items():
            for keyword in keywords:
                if keyword in searchable:
                    if trigger_id not in matches:
                        matches[trigger_id] = []
                    matches[trigger_id].append({
                        "repo": repo.get("full_name", ""),
                        "url": repo.get("html_url", ""),
                        "matched_keyword": keyword,
                        "confidence": "medium",
                    })
                    break

    return matches


# ---------------------------------------------------------------------------
# LLM extraction via Haiku
# ---------------------------------------------------------------------------

def analyze_with_haiku(
    org_meta: dict,
    repos: list[dict],
    lang_dist: dict,
    infra_signals: dict,
    company_name: str,
    persona: dict,
) -> dict:
    """Light Haiku pass to interpret the engineering footprint."""
    if not repos:
        return {"status": "insufficient_data", "reason": "No public repos to analyze"}

    client = anthropic.Anthropic()
    persona_ctx = _load_persona_context(persona)

    # Compact repo summaries for Haiku
    repo_summaries = []
    for r in repos:
        repo_summaries.append({
            "name": r.get("full_name", ""),
            "description": (r.get("description") or "")[:200],
            "language": r.get("language"),
            "stars": r.get("stargazers_count", 0),
            "forks": r.get("forks_count", 0),
            "updated_at": r.get("updated_at", ""),
            "topics": r.get("topics", []),
        })

    system_prompt = (
        "You are a technical analyst examining a company's public GitHub presence "
        "for a Verkada Solutions Engineer's pre-sales research tool.\n\n"
        "## Verkada Context (use this to judge relevance)\n"
        f"{persona_ctx}\n\n"
        "## Your Task\n"
        "Analyze the GitHub org data to infer signals relevant to a physical security sales conversation:\n"
        "1. Engineering scale and sophistication (org size, repo count, activity level)\n"
        "2. Cloud-native posture (Kubernetes, Terraform, Docker repos signal cloud-first IT)\n"
        "3. Security awareness (security-related repos, vulnerability scanning, compliance tools)\n"
        "4. IoT/embedded work (camera, sensor, edge computing repos — rare but gold)\n"
        "5. IT infrastructure patterns (monitoring, CI/CD maturity, config management)\n\n"
        "## Output Schema\n"
        "Output ONLY valid JSON with no markdown formatting. Use this exact schema:\n"
        '{"findings": [{'
        '"signal": "one-sentence description — MUST reference specific repos or data points", '
        '"evidence": ["org/repo-name-1", "org/repo-name-2"], '
        '"category": "one of: cloud_posture|security_awareness|scale_indicator|'
        'IT_maturity|iot_embedded|tech_stack|other", '
        '"confidence": "one of: high|medium|inference — '
        'high = directly evidenced by repo name/description/topics, '
        'medium = pattern across multiple repos, '
        'inference = interpretation requiring assumptions", '
        '"verkada_relevant": true/false'
        '}], '
        '"engineering_profile": "2-3 sentence summary of what this GitHub presence signals about the company\'s IT organization — reference specific repos"}\n\n'
        "## verkada_relevant Criteria\n"
        "Flag true ONLY if the finding directly relates to:\n"
        "- Cloud-first IT posture (signals receptivity to cloud-managed security)\n"
        "- IoT/embedded/camera/sensor work (physical security adjacency)\n"
        "- Security tooling or compliance repos (security-conscious org)\n"
        "- Infrastructure automation (signals IT team sophistication — easier Verkada deployment)\n"
        "Do NOT flag generic open-source activity or app development as verkada_relevant.\n\n"
        "## Anti-Genericness Rules (MANDATORY)\n"
        "- Every finding must cite SPECIFIC repos from the data. No vague claims.\n"
        "- If a finding could appear in any company's report, drop it.\n"
        "- Do NOT use hedging words (likely, potentially, may) unless paired with confidence: inference.\n"
        "- Public GitHub is a partial view — do not overclaim. Be explicit about what IS visible.\n"
        "- If the repos are too generic to extract meaningful signals, return:\n"
        '  {"findings": [], "engineering_profile": "insufficient_data", '
        '"status": "insufficient_data", "reason": "Public repos are primarily generic with no infrastructure-relevant patterns"}\n'
        "- Extract at most 8 findings. Quality over quantity.\n"
    )

    user_msg = (
        f"Company: {company_name}\n"
        f"GitHub org: {org_meta.get('login', '?')} ({org_meta.get('name', 'no name')})\n"
        f"Public repos: {org_meta.get('public_repos', '?')}\n"
        f"Language distribution: {json.dumps(lang_dist)}\n"
        f"Created: {org_meta.get('created_at', '?')}\n\n"
        f"Top {len(repo_summaries)} recently updated repos:\n{json.dumps(repo_summaries, indent=1)}"
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
    return SOURCES_DIR / company_slug / "github.json"


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

    retrieved_dt = datetime.fromisoformat(retrieved_at)
    age_days = (datetime.now(timezone.utc) - retrieved_dt).days
    if age_days > CACHE_TTL_DAYS:
        print(f"  [cache] github.json is {age_days}d old (TTL={CACHE_TTL_DAYS}d), refetching", file=sys.stderr)
        return None

    print(f"  [cache] github.json is {age_days}d old, within TTL", file=sys.stderr)
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

def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def fetch_github_data(company_name: str, *, org_override: str = "", force_refresh: bool = False) -> dict:
    """
    Full pipeline: resolve org → fetch metadata + repos → classify → Haiku analysis → cache.
    """
    company_slug = slugify(company_name)

    if not force_refresh:
        cached = read_cache(company_slug)
        if cached is not None:
            return cached

    persona = _load_persona()

    # Step 1: Resolve GitHub org
    if org_override:
        resp = _github_get(f"{GITHUB_API}/orgs/{org_override}")
        if resp.status_code == 200:
            org_meta = resp.json()
            print(f"  [github] using override org: {org_override}", file=sys.stderr)
        else:
            org_meta = None
    else:
        org_meta = resolve_github_org(company_name)

    if not org_meta:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "github",
            "status": "insufficient_data",
            "reason": (
                f"No GitHub organization found for '{company_name}'. "
                "This is common for non-tech companies, especially in Verkada's core verticals "
                "(K-12, healthcare, manufacturing, retail). Use --org to specify the org slug manually."
            ),
            "company": {"name": company_name},
            "org": {},
            "repos": [],
            "analysis": {},
        }
        write_cache(company_slug, result)
        return result

    org_login = org_meta["login"]
    rate_mode = "authenticated (5000/hr)" if GITHUB_TOKEN else "unauthenticated (60/hr)"
    print(f"  [github] rate limit mode: {rate_mode}", file=sys.stderr)

    # Step 2: Fetch repos
    repos = fetch_org_repos(org_login)
    print(f"  [github] fetched {len(repos)} repos", file=sys.stderr)

    # Step 3: Deterministic analysis (no LLM)
    lang_dist = compute_language_distribution(repos)
    infra_signals = detect_infra_signals(repos)
    trigger_matches = match_trigger_keywords(repos, persona)

    infra_hit_count = sum(len(v) for v in infra_signals.values())
    print(f"  [classify] {infra_hit_count} infra signal(s), {len(trigger_matches)} trigger(s) fired", file=sys.stderr)

    # Step 4: Haiku analysis
    haiku_analysis = analyze_with_haiku(org_meta, repos, lang_dist, infra_signals, company_name, persona)

    # Step 5: Normalize repos for cache (strip large fields)
    repo_entries = []
    for r in repos:
        repo_entries.append({
            "full_name": r.get("full_name", ""),
            "name": r.get("name", ""),
            "description": (r.get("description") or "")[:300],
            "html_url": r.get("html_url", ""),
            "language": r.get("language"),
            "stargazers_count": r.get("stargazers_count", 0),
            "forks_count": r.get("forks_count", 0),
            "updated_at": r.get("updated_at", ""),
            "created_at": r.get("created_at", ""),
            "topics": r.get("topics", []),
            "archived": r.get("archived", False),
        })

    # Step 6: Assemble and cache
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "github",
        "source_url": f"https://github.com/{org_login}",
        "company": {"name": company_name},
        "org": {
            "login": org_meta.get("login", ""),
            "name": org_meta.get("name", ""),
            "description": (org_meta.get("description") or "")[:500],
            "public_repos": org_meta.get("public_repos", 0),
            "public_members": org_meta.get("public_members_count", 0),
            "created_at": org_meta.get("created_at", ""),
            "updated_at": org_meta.get("updated_at", ""),
            "blog": org_meta.get("blog", ""),
            "location": org_meta.get("location", ""),
        },
        "repos": repo_entries,
        "language_distribution": lang_dist,
        "infra_signals": infra_signals,
        "trigger_matches": trigger_matches,
        "haiku_analysis": haiku_analysis,
        "summary": {
            "public_repos_total": org_meta.get("public_repos", 0),
            "repos_analyzed": len(repos),
            "top_languages": list(lang_dist.keys())[:5],
            "cloud_native_repos": len(infra_signals.get("cloud_native", [])),
            "security_repos": len(infra_signals.get("security_repos", [])),
            "iot_embedded_repos": len(infra_signals.get("iot_embedded", [])),
            "triggers_fired": list(trigger_matches.keys()),
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
        print("Usage: python github.py <company_name> [--org github_org] [--force]", file=sys.stderr)
        print("  e.g.: python github.py 'Shopify'", file=sys.stderr)
        print("        python github.py 'Target Corporation' --org target", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Optional: set GITHUB_TOKEN env var for 5000 req/hr (vs 60/hr unauthenticated)", file=sys.stderr)
        sys.exit(1)

    company_name = sys.argv[1]
    force = "--force" in sys.argv

    org_override = ""
    if "--org" in sys.argv:
        idx = sys.argv.index("--org")
        if idx + 1 < len(sys.argv):
            org_override = sys.argv[idx + 1]

    try:
        result = fetch_github_data(company_name, org_override=org_override, force_refresh=force)

        if result.get("status") == "insufficient_data":
            print(f"\n  {result['reason']}", file=sys.stderr)
        else:
            summary = result["summary"]
            org = result["org"]
            print(
                f"\n  Done: {result['company']['name']} → github.com/{org['login']}\n"
                f"  Public repos: {summary['public_repos_total']} (analyzed top {summary['repos_analyzed']})\n"
                f"  Top languages: {', '.join(summary['top_languages'])}\n"
                f"  Cloud-native repos: {summary['cloud_native_repos']}\n"
                f"  Security repos: {summary['security_repos']}\n"
                f"  IoT/embedded repos: {summary['iot_embedded_repos']}\n"
                f"  Triggers fired: {summary['triggers_fired'] or 'none'}\n"
                f"  Verkada-relevant findings: {summary['verkada_relevant_findings']}\n"
                f"  Cached to: sources/{slugify(company_name)}/github.json",
                file=sys.stderr,
            )

        print(json.dumps(result, indent=2, default=str))

    except requests.HTTPError as e:
        print(f"ERROR: GitHub API error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
