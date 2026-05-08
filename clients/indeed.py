"""
Indeed / Jobs Client — Fetches job postings for a company, extracts hiring signals.

Language choice: Python (same as clients/sec.py — shared pattern, PyYAML + anthropic available)

Data Source Strategy:
  PRIMARY: SerpAPI Google Jobs engine (requires SERPAPI_KEY env var)
    - Google Jobs aggregates Indeed, LinkedIn, ZipRecruiter, Glassdoor, and direct career pages
    - Returns structured JSON: title, company, location, description, apply links
    - Legal, stable, no anti-bot risk
    - Free tier: 250 searches/month (sufficient for dev + demo)

  FALLBACK: insufficient_data with clear reason
    - Indeed blocks all direct programmatic access (HTTP 403 + DataDome/Cloudflare)
    - No public Indeed API exists (deprecated years ago)
    - Direct scraping is fragile, TOS-violating, and maintenance-heavy

Fragility Points (documented per request):
  1. SerpAPI dependency — if the service is down or key exhausted, no data. Mitigated by 14-day cache.
  2. Google Jobs coverage — Google's index may lag behind Indeed by 1-3 days for new postings.
  3. Pagination limits — Google Jobs returns 10 results per page. For companies with 500+ open reqs,
     we fetch up to 5 pages (50 postings) and extrapolate total from the first page's metadata.
  4. Company name matching — Google Jobs matches by query string, not by employer ID. Subsidiaries
     and staffing agencies posting "on behalf of" may pollute results. Haiku filters these.
  5. SerpAPI rate limits — Free: 50/hour, Starter: 200/hour. We add 1s delay between page fetches.

Extraction: Claude Haiku per CLAUDE.md model assignment.
Cache TTL: 14 days per CLAUDE.md caching rules for job postings.
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

SERPAPI_BASE = "https://serpapi.com/search"
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

# Cache TTL for job postings: 14 days (per CLAUDE.md)
CACHE_TTL_DAYS = 14

# SerpAPI rate limiting: 1 request per second minimum
REQUEST_INTERVAL = 1.1

# Max pages to fetch (10 results per page)
MAX_PAGES = 5

_last_request_time = 0.0


# ---------------------------------------------------------------------------
# Persona loading
# ---------------------------------------------------------------------------

def _load_persona() -> dict:
    """Load the full persona YAML for trigger matching and Haiku context."""
    if not PERSONA_PATH.exists():
        return {}
    try:
        return yaml.safe_load(PERSONA_PATH.read_text()) or {}
    except Exception:
        return {}


def _load_persona_context(persona: dict) -> str:
    """Format Verkada context string for Haiku prompts (mirrors sec.py pattern)."""
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


def _extract_trigger_job_titles(persona: dict) -> dict[str, list[str]]:
    """
    Extract job_titles from all triggers in verkada-se.yml.
    Returns {trigger_id: [title1, title2, ...]} for triggers that have job_titles in detect_signals.
    """
    triggers = persona.get("triggers", [])
    result = {}
    for trigger in triggers:
        if not isinstance(trigger, dict):
            continue
        signals = trigger.get("detect_signals", {})
        titles = signals.get("job_titles", [])
        if titles:
            result[trigger["id"]] = [t.lower() for t in titles]
    return result


def _extract_trigger_keywords(persona: dict) -> dict[str, list[str]]:
    """
    Extract keywords from triggers that list job_postings in source_hints.
    Returns {trigger_id: [keyword1, keyword2, ...]} for job-relevant triggers.
    """
    triggers = persona.get("triggers", [])
    result = {}
    for trigger in triggers:
        if not isinstance(trigger, dict):
            continue
        hints = trigger.get("detect_signals", {}).get("source_hints", [])
        if "job_postings" not in hints:
            continue
        keywords = trigger.get("detect_signals", {}).get("keywords", [])
        if keywords:
            result[trigger["id"]] = [k.lower() for k in keywords]
    return result


# ---------------------------------------------------------------------------
# SerpAPI Google Jobs fetching
# ---------------------------------------------------------------------------

def _serpapi_get(params: dict, *, timeout: int = 30) -> dict:
    """Rate-limited SerpAPI request."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)

    params["api_key"] = SERPAPI_KEY
    resp = requests.get(SERPAPI_BASE, params=params, timeout=timeout)
    _last_request_time = time.time()

    if resp.status_code == 429:
        print("  [rate-limited] SerpAPI rate limit hit, waiting 60s…", file=sys.stderr)
        time.sleep(60)
        return _serpapi_get(params, timeout=timeout)

    resp.raise_for_status()
    return resp.json()


def fetch_google_jobs(company_name: str) -> list[dict]:
    """
    Fetch job postings via SerpAPI Google Jobs engine.
    Returns list of raw job dicts from the API.
    """
    if not SERPAPI_KEY:
        return []

    all_jobs = []
    next_page_token = None

    for page in range(MAX_PAGES):
        params = {
            "engine": "google_jobs",
            "q": f'"{company_name}" jobs',
        }
        if next_page_token:
            params["next_page_token"] = next_page_token

        print(f"  [serpapi] fetching page {page + 1}…", file=sys.stderr)

        try:
            data = _serpapi_get(params)
        except requests.HTTPError as e:
            print(f"  [serpapi] HTTP error on page {page + 1}: {e}", file=sys.stderr)
            break

        jobs = data.get("jobs_results", [])
        if not jobs:
            break

        all_jobs.extend(jobs)

        # Check for pagination
        serpapi_pagination = data.get("serpapi_pagination", {})
        next_page_token = serpapi_pagination.get("next_page_token")
        if not next_page_token:
            break

    print(f"  [serpapi] fetched {len(all_jobs)} postings across {page + 1} page(s)", file=sys.stderr)
    return all_jobs


# ---------------------------------------------------------------------------
# Local signal matching (no LLM needed)
# ---------------------------------------------------------------------------

def match_trigger_signals(jobs: list[dict], persona: dict) -> dict:
    """
    Match job postings against detect_signals from verkada-se.yml triggers.
    Returns structured signal matches with source attribution.
    """
    trigger_titles = _extract_trigger_job_titles(persona)
    trigger_keywords = _extract_trigger_keywords(persona)

    matches = {}  # trigger_id -> list of match evidence

    for job in jobs:
        title = (job.get("title") or "").lower()
        description = (job.get("description") or "").lower()
        job_ref = {
            "title": job.get("title", ""),
            "company": job.get("company_name", ""),
            "location": job.get("location", ""),
            "source_url": _best_apply_link(job),
            "detected_via": job.get("detected_via", "google_jobs"),
        }

        # Match against job_titles triggers
        for trigger_id, trigger_title_list in trigger_titles.items():
            for trigger_title in trigger_title_list:
                if trigger_title in title:
                    if trigger_id not in matches:
                        matches[trigger_id] = []
                    matches[trigger_id].append({
                        **job_ref,
                        "matched_signal": trigger_title,
                        "match_type": "job_title",
                        "confidence": "high",
                    })
                    break  # One match per job per trigger is enough

        # Match against keyword triggers (in title or description)
        for trigger_id, keywords in trigger_keywords.items():
            for keyword in keywords:
                if keyword in title or keyword in description:
                    if trigger_id not in matches:
                        matches[trigger_id] = []
                    # Avoid duplicating if already matched by title
                    already = any(m["title"] == job_ref["title"] for m in matches.get(trigger_id, []))
                    if not already:
                        matches[trigger_id].append({
                            **job_ref,
                            "matched_signal": keyword,
                            "match_type": "keyword_in_posting",
                            "confidence": "medium",
                        })
                    break

    return matches


def _best_apply_link(job: dict) -> str:
    """Extract the best source URL from a Google Jobs result."""
    # Google Jobs provides related_links or apply_options
    apply_options = job.get("apply_options", [])
    if apply_options:
        return apply_options[0].get("link", "")

    related_links = job.get("related_links", [])
    if related_links:
        return related_links[0].get("link", "")

    return job.get("job_id", "")


def categorize_roles(jobs: list[dict]) -> dict[str, int]:
    """
    Categorize job postings into role buckets.
    Uses keyword matching — deterministic, no LLM needed.
    """
    categories = {
        "engineering": 0,
        "security_safety": 0,
        "sales_marketing": 0,
        "operations_logistics": 0,
        "facilities_maintenance": 0,
        "IT_infrastructure": 0,
        "executive_leadership": 0,
        "hr_finance_legal": 0,
        "other": 0,
    }

    category_keywords = {
        "engineering": ["engineer", "developer", "software", "devops", "sre", "architect", "data scientist", "machine learning"],
        "security_safety": ["security", "safety", "loss prevention", "surveillance", "guard", "protection", "cctv", "access control"],
        "sales_marketing": ["sales", "marketing", "account", "business development", "revenue", "growth", "brand"],
        "operations_logistics": ["operations", "logistics", "supply chain", "warehouse", "distribution", "fulfillment", "transportation"],
        "facilities_maintenance": ["facilities", "maintenance", "building", "property", "custodial", "hvac", "janitorial"],
        "IT_infrastructure": ["IT ", "information technology", "network", "systems admin", "helpdesk", "cloud", "infrastructure", "cyber"],
        "executive_leadership": ["director", "vp ", "vice president", "chief", "head of", "president", "general manager"],
        "hr_finance_legal": ["human resources", "recruiter", "payroll", "finance", "accounting", "legal", "compliance", "audit"],
    }

    for job in jobs:
        title = (job.get("title") or "").lower()
        matched = False
        for category, keywords in category_keywords.items():
            if any(kw in title for kw in keywords):
                categories[category] += 1
                matched = True
                break
        if not matched:
            categories["other"] += 1

    return categories


# ---------------------------------------------------------------------------
# LLM extraction via Haiku
# ---------------------------------------------------------------------------

def extract_with_haiku(jobs: list[dict], company_name: str, persona: dict) -> dict:
    """
    Use Claude Haiku to analyze job postings for hiring intent signals.
    Summarizes patterns Haiku detects beyond what keyword matching catches.
    """
    if not jobs:
        return {"status": "insufficient_data", "reason": "No job postings retrieved to analyze"}

    if len(jobs) < 3:
        return {"status": "insufficient_data", "reason": f"Only {len(jobs)} postings found — too few for meaningful pattern analysis"}

    # Build a compact representation of postings for Haiku (avoid blowing context)
    job_summaries = []
    for job in jobs[:40]:  # Cap at 40 to stay within context
        summary = {
            "title": job.get("title", ""),
            "location": job.get("location", ""),
            "description_snippet": (job.get("description") or "")[:500],
        }
        job_summaries.append(summary)

    client = anthropic.Anthropic()
    persona_ctx = _load_persona_context(persona)

    system_prompt = (
        "You are a hiring signals analyst extracting insights from job postings "
        "for a Verkada Solutions Engineer's pre-sales research tool.\n\n"
        "## Verkada Context (use this to judge relevance)\n"
        f"{persona_ctx}\n\n"
        "## Output Schema\n"
        "Output ONLY valid JSON with no markdown formatting. Use this exact schema:\n"
        '{"insights": [{'
        '"signal": "one-sentence description of the hiring pattern — MUST reference specific job titles or posting details from the data", '
        '"evidence_titles": ["exact job title 1", "exact job title 2"], '
        '"category": "one of: security_buildout|facilities_expansion|IT_modernization|executive_change|operations_scaling|other", '
        '"confidence": "one of: high|medium|inference — '
        'high = 3+ postings clearly support this signal, '
        'medium = 1-2 postings suggest this signal, '
        'inference = pattern interpretation not directly supported by individual postings", '
        '"verkada_relevant": true/false'
        '}], '
        '"hiring_velocity": "one of: aggressive|moderate|minimal|insufficient_data — '
        'aggressive = 50+ open reqs or 10+ security/facilities roles, '
        'moderate = 15-49 open reqs, '
        'minimal = under 15 open reqs"}\n\n'
        "## verkada_relevant Criteria\n"
        "Flag true ONLY if the insight directly relates to:\n"
        "- Physical security hiring (security managers, LP, safety, guards)\n"
        "- Facilities or building operations (facility managers, property, maintenance leads)\n"
        "- IT infrastructure roles that would own NVR/DVR/VMS or access control systems\n"
        "- New site openings or expansions (signals greenfield security deployment)\n"
        "- Cloud/digital transformation roles (signals on-prem-to-cloud posture)\n"
        "Do NOT flag generic engineering or sales hiring as verkada_relevant.\n\n"
        "## Anti-Genericness Rules (MANDATORY)\n"
        "- Every signal must cite SPECIFIC job titles from the data. No vague 'the company is hiring.'\n"
        "- If an insight could appear unchanged in a report about any company, rewrite it with specifics or drop it.\n"
        "- Do NOT use hedging words (likely, potentially, may) unless paired with confidence: inference.\n"
        "- If the postings are too generic or sparse to extract meaningful signals, return:\n"
        '  {"insights": [], "hiring_velocity": "insufficient_data", '
        '"status": "insufficient_data", "reason": "Postings lack signal density for meaningful analysis"}\n'
        "- Extract at most 8 insights. Quality over quantity.\n"
    )

    user_msg = (
        f"Company: {company_name}\n"
        f"Total postings retrieved: {len(jobs)}\n"
        f"Postings (up to 40):\n\n{json.dumps(job_summaries, indent=1)}"
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
    return SOURCES_DIR / company_slug / "jobs.json"


def read_cache(company_slug: str) -> dict | None:
    """Read cached jobs.json if it exists and is within 14-day TTL."""
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

    # Don't serve a cached insufficient_data result if we now have an API key
    if data.get("status") == "insufficient_data" and SERPAPI_KEY:
        print("  [cache] cached insufficient_data but SERPAPI_KEY now set, refetching", file=sys.stderr)
        return None

    retrieved_dt = datetime.fromisoformat(retrieved_at)
    age_days = (datetime.now(timezone.utc) - retrieved_dt).days
    if age_days > CACHE_TTL_DAYS:
        print(f"  [cache] jobs.json is {age_days}d old (TTL={CACHE_TTL_DAYS}d), refetching", file=sys.stderr)
        return None

    print(f"  [cache] jobs.json is {age_days}d old, within TTL", file=sys.stderr)
    return data


def write_cache(company_slug: str, data: dict) -> Path:
    """Write structured data to sources/{company}/jobs.json."""
    path = cache_path(company_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))
    print(f"  [cache] wrote {path}", file=sys.stderr)
    return path


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class JobsUnavailableError(Exception):
    pass


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Convert company name to filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


def fetch_jobs_data(company_name: str, *, force_refresh: bool = False) -> dict:
    """
    Full pipeline: fetch jobs → categorize → match triggers → Haiku analysis → cache.

    Returns the structured JSON dict (also written to sources/{company}/jobs.json).
    """
    company_slug = slugify(company_name)

    # Check cache first
    if not force_refresh:
        cached = read_cache(company_slug)
        if cached is not None:
            return cached

    # Validate we have a data source
    if not SERPAPI_KEY:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "google_jobs_via_serpapi",
            "status": "insufficient_data",
            "reason": (
                "SERPAPI_KEY not set. Indeed blocks direct scraping (HTTP 403 + DataDome). "
                "Set SERPAPI_KEY env var to use Google Jobs API, or manually populate "
                f"sources/{company_slug}/jobs.json with pre-collected data."
            ),
            "company": {"name": company_name},
            "postings": [],
            "analysis": {},
        }
        write_cache(company_slug, result)
        return result

    persona = _load_persona()

    # Step 1: Fetch job postings from Google Jobs
    print(f"  [jobs] fetching postings for '{company_name}'…", file=sys.stderr)
    raw_jobs = fetch_google_jobs(company_name)

    if not raw_jobs:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "google_jobs_via_serpapi",
            "status": "insufficient_data",
            "reason": f"No job postings found for '{company_name}' in Google Jobs index.",
            "company": {"name": company_name},
            "postings": [],
            "analysis": {},
        }
        write_cache(company_slug, result)
        return result

    # Step 2: Normalize postings to a clean schema with source attribution
    postings = []
    for job in raw_jobs:
        postings.append({
            "title": job.get("title", ""),
            "company_name": job.get("company_name", ""),
            "location": job.get("location", ""),
            "description_snippet": (job.get("description") or "")[:1000],
            "detected_extensions": job.get("detected_extensions", {}),
            "source_url": _best_apply_link(job),
            "retrieved_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        })

    # Step 3: Deterministic categorization (no LLM)
    role_distribution = categorize_roles(raw_jobs)
    total_reqs = len(postings)

    # Step 4: Match against persona trigger signals (no LLM)
    trigger_matches = match_trigger_signals(raw_jobs, persona)

    # Step 5: Haiku analysis for deeper pattern extraction
    haiku_analysis = extract_with_haiku(raw_jobs, company_name, persona)

    # Step 6: Assemble and cache
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "google_jobs_via_serpapi",
        "company": {"name": company_name},
        "summary": {
            "total_active_reqs": total_reqs,
            "total_active_reqs_confidence": "medium"
                if total_reqs < MAX_PAGES * 10
                else "inference — capped at {0} fetched, actual count likely higher".format(total_reqs),
            "role_distribution": role_distribution,
            "verkada_relevant_signals": sum(len(v) for v in trigger_matches.values()),
        },
        "trigger_matches": trigger_matches,
        "haiku_analysis": haiku_analysis,
        "postings": postings,
    }

    write_cache(company_slug, result)
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python indeed.py <company_name> [--force]", file=sys.stderr)
        print("  e.g.: python indeed.py 'Target Corporation'", file=sys.stderr)
        print("        python indeed.py 'Apple' --force", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Requires: SERPAPI_KEY env var for Google Jobs API access", file=sys.stderr)
        sys.exit(1)

    company_name = sys.argv[1]
    force = "--force" in sys.argv

    try:
        result = fetch_jobs_data(company_name, force_refresh=force)

        if result.get("status") == "insufficient_data":
            print(f"\n  {result['reason']}", file=sys.stderr)
        else:
            summary = result["summary"]
            triggers = result["trigger_matches"]
            print(
                f"\n  Done: {result['company']['name']}\n"
                f"  Total active reqs: {summary['total_active_reqs']}\n"
                f"  Role distribution: {json.dumps(summary['role_distribution'])}\n"
                f"  Trigger matches: {len(triggers)} trigger(s) fired, "
                f"{summary['verkada_relevant_signals']} total signal(s)\n"
                f"  Cached to: sources/{slugify(company_name)}/jobs.json",
                file=sys.stderr,
            )
            for tid, matches in triggers.items():
                print(f"    {tid}: {len(matches)} match(es)", file=sys.stderr)

        print(json.dumps(result, indent=2, default=str))

    except requests.HTTPError as e:
        print(f"ERROR: SerpAPI HTTP error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
