"""
Reddit Client — Public JSON search for SLED/SE intelligence signals.

Data source: Reddit public JSON endpoints (no auth required)
  - Search: https://www.reddit.com/search.json?q=QUERY&limit=25&sort=new
  - Subreddit: https://www.reddit.com/r/SUBREDDIT/search.json?q=QUERY&restrict_sr=1
  - Rate limit: ~60 requests/minute for unauthenticated. We run 7 searches max.

Target subreddits (SLED/SE intel):
  - r/sysadmin — general IT admin, vendor complaints, infrastructure projects
  - r/k12sysadmin — K-12 IT, camera/access control discussions
  - r/healthIT — healthcare IT, HIPAA, security concerns
  - r/CCTV — surveillance systems, vendor comparisons
  - r/sysadminjobs — IT hiring signals
  - r/highereducation — campus safety, budget, admin discussions
  - r/all — general search for the company name

Fragility points:
  1. Rate limiting: Reddit returns 429 if >60 req/min. 7 searches = safe.
     But cached responses are best — 14-day TTL.
  2. Relevance: Reddit search is keyword-based, not semantic. Company names
     that match common words (e.g., "Target") will return noise. The Haiku
     pass filters for relevance.
  3. Low signal: Many companies will have 0 Reddit mentions. insufficient_data
     is the expected output for most targets.
  4. Selftext truncation: Reddit JSON returns truncated selftext. We get enough
     for signal detection but not full post content.
  5. No auth: Using public JSON endpoints. No OAuth needed but no access to
     private subreddits or removed posts.

Extraction: Deterministic keyword matching + Claude Haiku per CLAUDE.md model assignment.
Cache TTL: 14 days.
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

try:
    import anthropic
except ImportError:
    anthropic = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "sources"
PERSONA_PATH = PROJECT_ROOT / "persona" / "verkada-se.yml"

HAIKU_MODEL = "claude-haiku-4-5-20251001"
CACHE_TTL_DAYS = 14
REQUEST_INTERVAL = 2.0  # seconds between Reddit requests

REDDIT_SEARCH_URL = "https://www.reddit.com/search.json"
REDDIT_SUB_SEARCH_URL = "https://www.reddit.com/r/{subreddit}/search.json"

USER_AGENT = "PresalesResearch/1.0 (pre-sales research tool; contact: research@presales-tool.dev)"

TARGET_SUBREDDITS = [
    "sysadmin",
    "k12sysadmin",
    "healthIT",
    "CCTV",
    "sysadminjobs",
    "highereducation",
]

# Trigger-relevant keywords
SIGNAL_KEYWORDS = {
    "incident_recent_12mo": ["breach", "shooting", "theft", "break-in", "vandal", "assault", "robbery", "incident"],
    "legacy_nvr_dvr_refresh": ["nvr", "dvr", "analog", "coax", "replace", "upgrade", "end of life", "eol", "obsolete"],
    "vendor_consolidation_signal": ["consolidat", "single pane", "unified", "one platform", "too many vendor"],
    "capital_project_signal": ["new building", "construction", "renovation", "expansion", "bond", "capital"],
    "hiring_security_intensity": ["hiring", "security director", "ciso", "security manager", "open position"],
    "ndaa_compliance_pressure": ["ndaa", "hikvision", "dahua", "banned", "complian", "federal"],
    "multi_site_sprawl": ["multiple campus", "multiple location", "multi-site", "remote site", "branch"],
}

VENDOR_KEYWORDS = [
    "verkada", "avigilon", "genetec", "milestone", "axis", "hikvision", "dahua",
    "honeywell", "bosch", "pelco", "exacq", "march networks", "eagle eye",
    "rhombus", "openpath", "brivo", "lenel", "ccure", "gallagher", "s2",
]

_last_request_time = 0.0


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
# Reddit API
# ---------------------------------------------------------------------------

def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()


def search_reddit(query: str, subreddit: str | None = None, limit: int = 25) -> list[dict]:
    """Search Reddit for posts matching query. Returns list of post dicts."""
    _rate_limit()

    params = {
        "q": query,
        "limit": min(limit, 100),
        "sort": "new",
        "t": "year",  # last 12 months
    }

    if subreddit:
        url = REDDIT_SUB_SEARCH_URL.format(subreddit=subreddit)
        params["restrict_sr"] = "1"
    else:
        url = REDDIT_SEARCH_URL

    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 429:
            print(f"  [reddit] rate limited, waiting 10s...", file=sys.stderr)
            time.sleep(10)
            resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"  [reddit] HTTP {resp.status_code} for {subreddit or 'all'}", file=sys.stderr)
            return []
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [reddit] error: {e}", file=sys.stderr)
        return []

    posts = []
    for child in data.get("data", {}).get("children", []):
        if child.get("kind") != "t3":
            continue
        p = child["data"]
        posts.append({
            "id": p.get("id", ""),
            "title": p.get("title", ""),
            "subreddit": p.get("subreddit", ""),
            "score": p.get("score", 0),
            "num_comments": p.get("num_comments", 0),
            "created_utc": p.get("created_utc", 0),
            "date": datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc).strftime("%Y-%m-%d"),
            "url": f"https://www.reddit.com{p.get('permalink', '')}",
            "selftext_snippet": (p.get("selftext", "") or "")[:500],
            "author": p.get("author", ""),
            "is_self": p.get("is_self", True),
        })

    return posts


def search_all_subreddits(company_name: str) -> dict:
    """Run searches across target subreddits + r/all."""
    results = {}

    # General search
    print(f"  [reddit] searching r/all for '{company_name}'...", file=sys.stderr)
    results["all"] = search_reddit(company_name)

    # Targeted subreddit searches
    for sub in TARGET_SUBREDDITS:
        print(f"  [reddit] searching r/{sub}...", file=sys.stderr)
        results[sub] = search_reddit(company_name, subreddit=sub)

    return results


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

def detect_signals(posts: list[dict]) -> dict:
    """Detect trigger signals and vendor mentions across posts."""
    trigger_matches = {}
    vendor_mentions = {}

    for post in posts:
        text = f"{post['title']} {post['selftext_snippet']}".lower()

        # Check trigger keywords
        for trigger, keywords in SIGNAL_KEYWORDS.items():
            for kw in keywords:
                if kw in text:
                    if trigger not in trigger_matches:
                        trigger_matches[trigger] = []
                    trigger_matches[trigger].append({
                        "post_id": post["id"],
                        "title": post["title"][:100],
                        "keyword": kw,
                        "subreddit": post["subreddit"],
                    })
                    break  # one match per trigger per post

        # Check vendor mentions
        for vendor in VENDOR_KEYWORDS:
            if vendor in text:
                if vendor not in vendor_mentions:
                    vendor_mentions[vendor] = []
                vendor_mentions[vendor].append({
                    "post_id": post["id"],
                    "title": post["title"][:100],
                    "subreddit": post["subreddit"],
                })

    return {"trigger_matches": trigger_matches, "vendor_mentions": vendor_mentions}


# ---------------------------------------------------------------------------
# Haiku analysis
# ---------------------------------------------------------------------------

def analyze_with_haiku(posts: list[dict], company_name: str, persona: dict) -> dict:
    """Light Haiku pass to identify pain signals, vendor mentions, or staffing refs."""
    if not anthropic or not os.environ.get("ANTHROPIC_API_KEY"):
        return {"status": "skipped", "reason": "ANTHROPIC_API_KEY not set"}

    if not posts:
        return {"status": "no_posts"}

    # Limit to top 20 most relevant posts
    sorted_posts = sorted(posts, key=lambda p: p["score"] + p["num_comments"], reverse=True)[:20]
    posts_text = json.dumps([{
        "title": p["title"],
        "subreddit": p["subreddit"],
        "score": p["score"],
        "comments": p["num_comments"],
        "date": p["date"],
        "snippet": p["selftext_snippet"][:200],
    } for p in sorted_posts], indent=2)

    triggers = persona.get("triggers", [])
    trigger_names = [t.get("name", "") for t in triggers if isinstance(t, dict)]

    prompt = f"""Analyze these Reddit posts mentioning "{company_name}" for pre-sales intelligence.

POSTS:
{posts_text}

TRIGGERS from persona: {', '.join(trigger_names)}

Identify:
1. Pain signals (complaints about security, IT infrastructure, vendor issues)
2. Vendor mentions (which security/IT vendors are discussed)
3. Leadership/staffing references (hiring, departures, restructuring)
4. Budget/project signals (new construction, renovations, RFPs)

Return JSON:
{{
  "pain_signals": [{{"post_title": "...", "signal": "...", "trigger": "..."}}],
  "vendor_mentions": [{{"vendor": "...", "context": "positive|negative|neutral"}}],
  "staffing_signals": [{{"signal": "..."}}],
  "summary": "one paragraph"
}}"""

    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        json_match = re.search(r'\{.*\}', text, re.S)
        if json_match:
            return json.loads(json_match.group(0))
        return {"status": "parse_error", "raw": text[:500]}
    except Exception as e:
        return {"status": "extraction_error", "reason": str(e)}


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate_posts(subreddit_results: dict) -> list[dict]:
    """Merge posts from all subreddit searches, deduplicate by post ID."""
    seen = set()
    unique = []
    for sub, posts in subreddit_results.items():
        for post in posts:
            if post["id"] not in seen:
                seen.add(post["id"])
                unique.append(post)
    return unique


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def cache_path(company_slug: str) -> Path:
    return SOURCES_DIR / company_slug / "reddit.json"


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

def fetch_reddit_data(company_name: str, *, force_refresh: bool = False) -> dict:
    """Full pipeline: search 7 subreddits → dedup → signal detection → Haiku → cache."""
    company_slug = slugify(company_name)

    if not force_refresh:
        cached = read_cache(company_slug)
        if cached is not None:
            return cached

    # Search
    subreddit_results = search_all_subreddits(company_name)

    # Deduplicate
    all_posts = deduplicate_posts(subreddit_results)

    # Per-subreddit counts
    sub_counts = {sub: len(posts) for sub, posts in subreddit_results.items()}
    total_raw = sum(sub_counts.values())
    print(f"  [reddit] {total_raw} raw results → {len(all_posts)} unique posts", file=sys.stderr)

    if not all_posts:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "reddit",
            "source_url": "https://www.reddit.com/search",
            "status": "insufficient_data",
            "reason": f"No Reddit posts found mentioning '{company_name}' in the last 12 months across {len(TARGET_SUBREDDITS) + 1} subreddits.",
            "company": {"name": company_name},
            "subreddit_counts": sub_counts,
        }
        write_cache(company_slug, result)
        return result

    # Signal detection
    signals = detect_signals(all_posts)

    # Haiku analysis
    persona = _load_persona()
    haiku_analysis = analyze_with_haiku(all_posts, company_name, persona)

    # Sort by score+comments for output
    all_posts.sort(key=lambda p: p["score"] + p["num_comments"], reverse=True)

    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "reddit",
        "source_url": "https://www.reddit.com/search",
        "company": {"name": company_name},
        "subreddit_counts": sub_counts,
        "posts": all_posts[:50],  # cap at 50 most relevant
        "signals": signals,
        "haiku_analysis": haiku_analysis,
        "summary": {
            "total_posts": len(all_posts),
            "subreddits_with_hits": sum(1 for c in sub_counts.values() if c > 0),
            "triggers_fired": list(signals["trigger_matches"].keys()),
            "vendors_mentioned": list(signals["vendor_mentions"].keys()),
            "source_quality": "secondary",
            "confidence": "medium" if len(all_posts) >= 3 else "inference",
        },
    }

    write_cache(company_slug, result)
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python reddit.py <company_name> [--force]", file=sys.stderr)
        print("  e.g.: python reddit.py 'Atlanta Public Schools'", file=sys.stderr)
        print("        python reddit.py 'Georgia Tech' --force", file=sys.stderr)
        print("", file=sys.stderr)
        print("  No API key needed. Searches Reddit public JSON endpoints.", file=sys.stderr)
        sys.exit(1)

    company_name = sys.argv[1]
    force = "--force" in sys.argv

    result = fetch_reddit_data(company_name, force_refresh=force)

    if result.get("status") == "insufficient_data":
        print(f"\n  {result['reason']}", file=sys.stderr)
    else:
        summary = result["summary"]
        print(
            f"\n  Done: {result['company']['name']}\n"
            f"  Total posts: {summary['total_posts']} (deduplicated)\n"
            f"  Subreddits with hits: {summary['subreddits_with_hits']}\n"
            f"  Counts: {json.dumps(result['subreddit_counts'])}\n"
            f"  Triggers fired: {summary['triggers_fired'] or 'none'}\n"
            f"  Vendors mentioned: {summary['vendors_mentioned'] or 'none'}\n"
            f"  Cached to: sources/{slugify(company_name)}/reddit.json",
            file=sys.stderr,
        )

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
