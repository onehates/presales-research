"""
News Client — Verkada-tuned news search via Tavily API for pre-sales discovery signals.

Language choice: Python (shared pattern with clients/sec.py, indeed.py, crtsh.py, github.py)

Data source: Tavily Search API (https://api.tavily.com/search)
  - Requires TAVILY_API_KEY env var
  - Free tier: 1,000 searches/month
  - 5 searches per company (one per query angle) = ~200 companies/month on free tier
  - Returns: title, url, content snippet, published_date, relevance_score

Query strategy:
  Five Verkada-tuned search angles, run sequentially:
    1. Security incidents (breach, theft, shooting, violence)
    2. Facility expansion (new building, construction, campus)
    3. Security leadership hires (CISO, Director of Security)
    4. Cybersecurity events (data breach, ransomware)
    5. Corporate changes (acquisition, merger, new HQ)

  These are NOT generic news queries. Each angle maps to specific triggers in
  verkada-se.yml: incident_recent_12mo, capital_project_signal, hiring_security_intensity,
  executive_leadership_change, etc.

Fragility points:
  1. Tavily quota: 1,000 searches/month free, 5 per company = ~200 companies/month.
     Mitigated by 7-day cache. For demo prep, run once and cache holds through interview.
  2. Search query bias: queries are tuned toward security/incident/expansion news, which
     biases results toward sensational events. This is intentional — SE discovery cares about
     pain signals, not PR fluff. But it means the news section will skew negative.
  3. Low news volume: private companies, small orgs, and non-US companies may produce
     0-2 results across all 5 queries. insufficient_data is the expected output.
  4. Published date accuracy: Tavily extracts dates from page metadata, which is sometimes
     missing or wrong. Treat published_date as medium confidence.
  5. Duplicate results: the same article may appear across multiple query angles.
     Deduplicated by URL before Haiku synthesis.

Extraction: Claude Haiku per CLAUDE.md model assignment.
Cache TTL: 7 days per CLAUDE.md caching rules for news.
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

TAVILY_API_URL = "https://api.tavily.com/search"
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

CACHE_TTL_DAYS = 7

# Rate-limit: Tavily has no documented per-second limit, but be polite
REQUEST_INTERVAL = 1.0

_last_request_time = 0.0

# ---------------------------------------------------------------------------
# Query angles — each maps to specific verkada-se.yml triggers
# ---------------------------------------------------------------------------

QUERY_ANGLES = [
    {
        "id": "security_incidents",
        "template": '"{company}" security incident OR breach OR theft OR shooting OR violence',
        "maps_to_triggers": ["incident_recent_12mo"],
        "description": "Physical security incidents, workplace violence, theft",
    },
    {
        "id": "facility_expansion",
        "template": '"{company}" new facility OR expansion OR construction OR campus OR "new building"',
        "maps_to_triggers": ["capital_project_signal"],
        "description": "New construction, facility expansion, campus projects",
    },
    {
        "id": "security_leadership",
        "template": '"{company}" CISO OR "Director of Security" OR "Chief Security Officer" OR "VP of Safety" hired OR appointed OR joins',
        "maps_to_triggers": ["hiring_security_intensity", "executive_leadership_change"],
        "description": "Security/safety leadership hires and appointments",
    },
    {
        "id": "cybersecurity",
        "template": '"{company}" cybersecurity OR "data breach" OR ransomware OR "security vulnerability"',
        "maps_to_triggers": ["incident_recent_12mo"],
        "description": "Cybersecurity events (often trigger physical security reviews)",
    },
    {
        "id": "corporate_changes",
        "template": '"{company}" acquisition OR merger OR "new headquarters" OR restructuring OR "office consolidation"',
        "maps_to_triggers": ["capital_project_signal", "vendor_consolidation_signal", "executive_leadership_change"],
        "description": "M&A, HQ moves, restructuring (signals greenfield security needs)",
    },
]


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


def _extract_trigger_keywords(persona: dict) -> dict[str, list[str]]:
    """Extract keywords from triggers that include 'news' in source_hints."""
    triggers = persona.get("triggers", [])
    result = {}
    for trigger in triggers:
        if not isinstance(trigger, dict):
            continue
        hints = trigger.get("detect_signals", {}).get("source_hints", [])
        if "news" not in hints:
            continue
        keywords = trigger.get("detect_signals", {}).get("keywords", [])
        if keywords:
            result[trigger["id"]] = [k.lower() for k in keywords]
    return result


# ---------------------------------------------------------------------------
# Tavily API
# ---------------------------------------------------------------------------

def _tavily_search(query: str, *, max_results: int = 10) -> dict:
    """Execute a single Tavily search query."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }

    resp = requests.post(TAVILY_API_URL, json=payload, timeout=30)
    _last_request_time = time.time()

    if resp.status_code == 429:
        print("  [tavily] rate limited, waiting 10s…", file=sys.stderr)
        time.sleep(10)
        return _tavily_search(query, max_results=max_results)

    if resp.status_code == 401:
        return {"error": "Invalid or missing TAVILY_API_KEY"}

    resp.raise_for_status()
    return resp.json()


def fetch_news(company_name: str) -> dict[str, list[dict]]:
    """
    Run all 5 query angles against Tavily and return results grouped by angle.
    Returns {angle_id: [result1, result2, ...]}
    """
    if not TAVILY_API_KEY:
        return {}

    results_by_angle = {}

    for angle in QUERY_ANGLES:
        query = angle["template"].replace("{company}", company_name)
        print(f"  [tavily] {angle['id']}: {query[:80]}…", file=sys.stderr)

        try:
            data = _tavily_search(query, max_results=8)
        except requests.HTTPError as e:
            print(f"  [tavily] HTTP error for {angle['id']}: {e}", file=sys.stderr)
            results_by_angle[angle["id"]] = []
            continue

        if "error" in data:
            print(f"  [tavily] error: {data['error']}", file=sys.stderr)
            results_by_angle[angle["id"]] = []
            continue

        raw_results = data.get("results", [])
        results_by_angle[angle["id"]] = raw_results

    total = sum(len(v) for v in results_by_angle.values())
    print(f"  [tavily] {total} total results across {len(QUERY_ANGLES)} angles", file=sys.stderr)
    return results_by_angle


# ---------------------------------------------------------------------------
# Deduplication and normalization
# ---------------------------------------------------------------------------

def deduplicate_results(results_by_angle: dict[str, list[dict]]) -> list[dict]:
    """
    Flatten and deduplicate results across all query angles by URL.
    Preserves which angle(s) each result came from.
    """
    seen_urls = {}  # url -> result dict
    for angle_id, results in results_by_angle.items():
        for r in results:
            url = r.get("url", "")
            if not url:
                continue
            if url in seen_urls:
                # Add this angle to existing entry
                seen_urls[url]["found_in_angles"].append(angle_id)
            else:
                seen_urls[url] = {
                    "title": r.get("title", ""),
                    "url": url,
                    "content": (r.get("content") or "")[:1500],
                    "published_date": r.get("published_date", ""),
                    "score": r.get("score", 0),
                    "found_in_angles": [angle_id],
                }

    # Sort by score descending
    deduped = sorted(seen_urls.values(), key=lambda x: -x.get("score", 0))
    return deduped


# ---------------------------------------------------------------------------
# Deterministic trigger matching (no LLM)
# ---------------------------------------------------------------------------

def match_trigger_keywords(articles: list[dict], persona: dict) -> dict:
    """Match article content against trigger keywords from verkada-se.yml."""
    trigger_keywords = _extract_trigger_keywords(persona)
    matches = {}

    for article in articles:
        text = f"{article.get('title', '')} {article.get('content', '')}".lower()

        for trigger_id, keywords in trigger_keywords.items():
            for keyword in keywords:
                if keyword in text:
                    if trigger_id not in matches:
                        matches[trigger_id] = []
                    # Avoid duplicate articles per trigger
                    already = any(m["url"] == article["url"] for m in matches[trigger_id])
                    if not already:
                        matches[trigger_id].append({
                            "title": article.get("title", ""),
                            "url": article.get("url", ""),
                            "matched_keyword": keyword,
                            "confidence": "high",
                        })
                    break  # One keyword match per article per trigger

    return matches


# ---------------------------------------------------------------------------
# LLM extraction via Haiku
# ---------------------------------------------------------------------------

def synthesize_with_haiku(articles: list[dict], company_name: str, persona: dict) -> dict:
    """
    Use Haiku to synthesize news articles into structured findings.
    Each finding must cite the specific Tavily result it came from.
    """
    if not articles:
        return {"status": "insufficient_data", "reason": "No news articles retrieved to analyze"}

    if len(articles) < 2:
        return {
            "status": "insufficient_data",
            "reason": f"Only {len(articles)} article(s) found — too few for meaningful synthesis",
        }

    client = anthropic.Anthropic()
    persona_ctx = _load_persona_context(persona)

    # Build article list for Haiku (cap at 25 to manage context)
    article_summaries = []
    for i, a in enumerate(articles[:25]):
        article_summaries.append({
            "index": i + 1,
            "title": a.get("title", ""),
            "url": a.get("url", ""),
            "published_date": a.get("published_date", "unknown"),
            "content_snippet": a.get("content", "")[:800],
            "query_angles": a.get("found_in_angles", []),
        })

    system_prompt = (
        "You are a news analyst synthesizing recent news articles about a company "
        "for a Verkada Solutions Engineer's pre-sales research tool.\n\n"
        "## Verkada Context (use this to judge relevance)\n"
        f"{persona_ctx}\n\n"
        "## Your Task\n"
        "Synthesize the news articles into discrete findings relevant to a physical security "
        "sales conversation. Focus on:\n"
        "1. Security incidents (theft, violence, breach) — highest priority\n"
        "2. Facility changes (new buildings, expansions, renovations, HQ moves)\n"
        "3. Leadership changes (especially security/safety/IT leadership)\n"
        "4. Cybersecurity events (often trigger physical security reviews)\n"
        "5. M&A activity (acquisitions add sites needing security integration)\n"
        "6. Regulatory or compliance events affecting physical security\n\n"
        "## Output Schema\n"
        "Output ONLY valid JSON with no markdown formatting. Use this exact schema:\n"
        '{"findings": [{'
        '"headline": "one-sentence finding — MUST include specific details (dates, names, locations) from the article", '
        '"detail": "2-3 sentence elaboration with specifics from the source article", '
        '"source_title": "exact title of the article this finding comes from", '
        '"source_url": "exact URL of the article", '
        '"source_published_date": "published date from article or null", '
        '"category": "one of: security_incident|facility_expansion|leadership_change|'
        'cybersecurity|corporate_change|regulatory|other", '
        '"confidence": "one of: high|medium|inference — '
        'high = finding directly stated in article with specifics, '
        'medium = finding implied by article context, '
        'inference = interpretation connecting article to physical security relevance", '
        '"verkada_relevant": true/false, '
        '"relevance_reason": "if verkada_relevant is true, explain WHY in one sentence — must tie to a specific Verkada product line or displacement scenario"'
        '}], '
        '"news_density": "one of: high|moderate|low|insufficient_data — '
        'high = 10+ relevant articles with strong signals, '
        'moderate = 4-9 relevant articles, '
        'low = 1-3 relevant articles"}\n\n'
        "## verkada_relevant Criteria\n"
        "Flag true ONLY if the finding directly creates a physical security sales opportunity:\n"
        "- Security incident → need for better surveillance/detection/response\n"
        "- New facility → greenfield security deployment opportunity\n"
        "- Security leadership hire → new decision maker who will audit existing infrastructure\n"
        "- Cybersecurity breach → often triggers physical security review alongside cyber\n"
        "- Acquisition → new sites need security integration under one platform\n"
        "Do NOT flag generic corporate news as verkada_relevant.\n\n"
        "## Anti-Genericness Rules (MANDATORY)\n"
        "- Every finding must cite a SPECIFIC article with its exact title and URL.\n"
        "- Headlines must include specific details: dates, names, dollar amounts, locations.\n"
        "- If a finding could appear in a report about any company, drop it.\n"
        "- Do NOT generate findings not supported by the articles provided.\n"
        "- Do NOT use hedging words (likely, potentially, may) unless paired with confidence: inference.\n"
        "- If the articles are too generic or irrelevant to extract meaningful signals, return:\n"
        '  {"findings": [], "news_density": "insufficient_data", '
        '"status": "insufficient_data", "reason": "Articles lack specificity for physical security discovery"}\n'
        "- Extract at most 10 findings. Quality over quantity.\n"
        "- NEVER invent details not present in the source articles.\n"
    )

    user_msg = (
        f"Company: {company_name}\n"
        f"Total deduplicated articles: {len(articles)}\n"
        f"Articles (up to 25):\n\n{json.dumps(article_summaries, indent=1)}"
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
    return SOURCES_DIR / company_slug / "news.json"


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

    # Don't serve cached insufficient_data if we now have a key
    if data.get("status") == "insufficient_data" and TAVILY_API_KEY:
        reason = data.get("reason", "")
        if "TAVILY_API_KEY" in reason:
            print("  [cache] cached insufficient_data but TAVILY_API_KEY now set, refetching", file=sys.stderr)
            return None

    retrieved_dt = datetime.fromisoformat(retrieved_at)
    age_days = (datetime.now(timezone.utc) - retrieved_dt).days
    if age_days > CACHE_TTL_DAYS:
        print(f"  [cache] news.json is {age_days}d old (TTL={CACHE_TTL_DAYS}d), refetching", file=sys.stderr)
        return None

    print(f"  [cache] news.json is {age_days}d old, within TTL", file=sys.stderr)
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


def fetch_news_data(company_name: str, *, force_refresh: bool = False) -> dict:
    """
    Full pipeline: Tavily search (5 angles) → dedup → trigger match → Haiku synthesis → cache.
    """
    company_slug = slugify(company_name)

    if not force_refresh:
        cached = read_cache(company_slug)
        if cached is not None:
            return cached

    # Validate we have an API key
    if not TAVILY_API_KEY:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "tavily",
            "status": "insufficient_data",
            "reason": (
                "TAVILY_API_KEY not set. Set the env var to use Tavily search API. "
                "Free tier: 1,000 searches/month (5 per company = ~200 companies/month). "
                f"Or manually populate sources/{company_slug}/news.json."
            ),
            "company": {"name": company_name},
            "articles": [],
            "haiku_analysis": {},
        }
        write_cache(company_slug, result)
        return result

    persona = _load_persona()

    # Step 1: Run 5 Tavily searches
    print(f"  [news] searching for '{company_name}' across {len(QUERY_ANGLES)} angles…", file=sys.stderr)
    results_by_angle = fetch_news(company_name)

    # Step 2: Deduplicate across angles
    articles = deduplicate_results(results_by_angle)
    print(f"  [news] {sum(len(v) for v in results_by_angle.values())} raw → {len(articles)} deduplicated articles", file=sys.stderr)

    if not articles:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "tavily",
            "status": "insufficient_data",
            "reason": f"No news articles found for '{company_name}' across any query angle. Company may have low public news volume.",
            "company": {"name": company_name},
            "query_angles_used": [a["id"] for a in QUERY_ANGLES],
            "articles": [],
            "haiku_analysis": {},
        }
        write_cache(company_slug, result)
        return result

    # Step 3: Deterministic trigger matching (no LLM)
    trigger_matches = match_trigger_keywords(articles, persona)
    trigger_hit_count = sum(len(v) for v in trigger_matches.values())
    print(f"  [triggers] {len(trigger_matches)} trigger(s) fired, {trigger_hit_count} total match(es)", file=sys.stderr)

    # Step 4: Haiku synthesis
    haiku_analysis = synthesize_with_haiku(articles, company_name, persona)

    # Step 5: Per-angle result counts for diagnostics
    angle_counts = {}
    for angle in QUERY_ANGLES:
        angle_counts[angle["id"]] = len(results_by_angle.get(angle["id"], []))

    # Step 6: Assemble and cache
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "tavily",
        "company": {"name": company_name},
        "query_angles_used": [a["id"] for a in QUERY_ANGLES],
        "results_per_angle": angle_counts,
        "articles": articles,
        "trigger_matches": trigger_matches,
        "haiku_analysis": haiku_analysis,
        "summary": {
            "total_articles": len(articles),
            "articles_with_dates": sum(1 for a in articles if a.get("published_date")),
            "triggers_fired": list(trigger_matches.keys()),
            "trigger_match_count": trigger_hit_count,
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
        print("Usage: python news.py <company_name> [--force]", file=sys.stderr)
        print("  e.g.: python news.py 'Target Corporation'", file=sys.stderr)
        print("        python news.py 'Apple' --force", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Requires: TAVILY_API_KEY env var", file=sys.stderr)
        print("  Free tier: 1,000 searches/month (5 per company)", file=sys.stderr)
        sys.exit(1)

    company_name = sys.argv[1]
    force = "--force" in sys.argv

    try:
        result = fetch_news_data(company_name, force_refresh=force)

        if result.get("status") == "insufficient_data":
            print(f"\n  {result['reason']}", file=sys.stderr)
        else:
            summary = result["summary"]
            print(
                f"\n  Done: {result['company']['name']}\n"
                f"  Total articles: {summary['total_articles']} (deduplicated)\n"
                f"  Results per angle: {json.dumps(result['results_per_angle'])}\n"
                f"  Triggers fired: {summary['triggers_fired'] or 'none'}\n"
                f"  Trigger matches: {summary['trigger_match_count']}\n"
                f"  Verkada-relevant findings: {summary['verkada_relevant_findings']}\n"
                f"  Cached to: sources/{slugify(company_name)}/news.json",
                file=sys.stderr,
            )

        print(json.dumps(result, indent=2, default=str))

    except requests.HTTPError as e:
        print(f"ERROR: Tavily API error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
