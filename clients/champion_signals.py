"""
Champion Signals Client — Deep public signal gathering per named individual.

For each named individual from leadership.json, gathers candidate-level signals:
  1. Tenure inference: Tavily search for hire/appointment dates
  2. Career arc: Tavily search for prior employers, cross-referenced against
     persona vendor_alumni_indicators and modern_stack_orgs
  3. Public commentary: Tavily search for speaking, writing, media activity
  4. Topic affinity: Haiku pass to extract topics from public commentary,
     scored against persona champion_topic_signals
  5. Authority signals: Title parsing for director+ level, budget quotes

Data source: Tavily Search API + Claude Haiku
  - Uses TAVILY_API_KEY (same as news client)
  - 3 Tavily searches per individual × ~5 individuals = ~15 searches per company
  - Haiku used for topic extraction (per CLAUDE.md model assignment)

Fragility points:
  1. Tavily quota: 15 searches per company, ~66 companies/month on free tier.
     Leadership pages don't change fast, so 30-day cache is appropriate.
  2. Name disambiguation: Common names (e.g., "Jim Hall") produce noisy results.
     Mitigated by including org name in every query.
  3. Public voice data is sparse for non-executive roles. Most directors/managers
     don't give keynotes or write articles. Expected result: 0.0 public_voice for
     most candidates. This is fine — it means the signal doesn't help, not that
     the candidate is bad.
  4. Career arc data from news searches is incomplete. LinkedIn would be better
     but is not scraped. Career arc scores will be inference-quality at best.

Cache TTL: 30 days (aligned with leadership.json).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
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
CACHE_TTL_DAYS = 30

TAVILY_API_URL = "https://api.tavily.com/search"
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
REQUEST_INTERVAL = 1.0
_last_request_time = 0.0


# ---------------------------------------------------------------------------
# Tavily helper (reuses pattern from news.py)
# ---------------------------------------------------------------------------

def _tavily_search(query: str, *, max_results: int = 5) -> list[dict]:
    """Execute a Tavily search, return list of results."""
    global _last_request_time

    if not TAVILY_API_KEY:
        return []

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

    try:
        resp = requests.post(TAVILY_API_URL, json=payload, timeout=30)
        _last_request_time = time.time()

        if resp.status_code == 429:
            time.sleep(10)
            return _tavily_search(query, max_results=max_results)
        if resp.status_code == 401:
            return []
        resp.raise_for_status()
        return resp.json().get("results", [])
    except (requests.RequestException, Exception):
        return []


# ---------------------------------------------------------------------------
# Persona loading
# ---------------------------------------------------------------------------

def _load_champion_criteria() -> dict:
    """Load champion_criteria from persona file."""
    if not PERSONA_PATH.exists():
        return {}
    try:
        persona = yaml.safe_load(PERSONA_PATH.read_text()) or {}
        return persona.get("champion_criteria", {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Signal gathering per individual
# ---------------------------------------------------------------------------

def _gather_tenure_signal(name: str, org: str) -> dict:
    """Search for when this person joined/was appointed."""
    query = f'"{name}" "{org}" (joined OR appointed OR named OR new OR hired)'
    results = _tavily_search(query, max_results=5)

    tenure_evidence = []
    recency_score = 0.0

    for r in results:
        content = (r.get("content") or "").lower()
        title_text = (r.get("title") or "").lower()
        combined = f"{title_text} {content}"

        # Look for date patterns near hire/join language
        if name.lower().split()[-1] in combined:  # Last name appears
            hire_words = ["joined", "appointed", "named", "hired", "promoted",
                          "selected", "new"]
            if any(hw in combined for hw in hire_words):
                tenure_evidence.append({
                    "source_url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "snippet": (r.get("content") or "")[:200],
                })

                # Check recency: look for year references
                year_match = re.search(r'20(2[4-6])', combined)
                if year_match:
                    year = int("20" + year_match.group(1))
                    current_year = datetime.now().year
                    months_ago = (current_year - year) * 12
                    if months_ago <= 18:
                        recency_score = max(recency_score, 0.25)
                    elif months_ago <= 36:
                        recency_score = max(recency_score, 0.10)

    return {
        "recency_score": recency_score,
        "evidence": tenure_evidence[:3],
    }


def _gather_career_arc(name: str, org: str, criteria: dict) -> dict:
    """Search for prior employers and career trajectory."""
    query = f'"{name}" (previously OR formerly OR "joined from" OR "prior to" OR "before joining")'
    results = _tavily_search(query, max_results=5)

    prior_employers = []
    career_arc_score = 0.0
    evidence = []

    vendor_alumni = [v.lower() for v in criteria.get("vendor_alumni_indicators", [])]
    modern_orgs = [v.lower() for v in criteria.get("modern_stack_orgs", [])]

    for r in results:
        content = (r.get("content") or "")
        if name.split()[-1].lower() not in content.lower():
            continue

        evidence.append({
            "source_url": r.get("url", ""),
            "title": r.get("title", ""),
            "snippet": content[:200],
        })

        content_lower = content.lower()
        # Check for vendor alumni
        for vendor in vendor_alumni:
            if vendor in content_lower:
                prior_employers.append(vendor)
                career_arc_score = max(career_arc_score, 0.15)

        # Check for modern stack org indicators
        for indicator in modern_orgs:
            if indicator in content_lower:
                career_arc_score = max(career_arc_score, 0.10)

        # Generic cloud/modern signals
        modern_signals = ["cloud", "saas", "digital transformation", "modernization"]
        if any(s in content_lower for s in modern_signals):
            career_arc_score = max(career_arc_score, 0.08)

    return {
        "career_arc_score": career_arc_score,
        "prior_employers_detected": prior_employers,
        "evidence": evidence[:3],
    }


def _gather_public_voice(name: str, org: str) -> dict:
    """Search for public speaking, writing, and media activity."""
    queries = [
        f'"{name}" (speaker OR keynote OR panel OR conference)',
        f'"{name}" (podcast OR interview OR "quoted" OR "said")',
        f'"{name}" (article OR authored OR wrote OR "op-ed")',
    ]

    all_results = []
    for q in queries:
        results = _tavily_search(q, max_results=3)
        for r in results:
            if name.split()[-1].lower() in (r.get("content") or "").lower():
                all_results.append(r)

    # Deduplicate by URL
    seen = set()
    unique = []
    for r in all_results:
        url = r.get("url", "")
        if url not in seen:
            seen.add(url)
            unique.append(r)

    # Score based on volume
    count = len(unique)
    if count >= 5:
        public_voice_score = 0.15
    elif count >= 3:
        public_voice_score = 0.12
    elif count >= 1:
        public_voice_score = 0.08
    else:
        public_voice_score = 0.0

    evidence = [{
        "source_url": r.get("url", ""),
        "title": r.get("title", ""),
        "snippet": (r.get("content") or "")[:150],
    } for r in unique[:5]]

    return {
        "public_voice_score": public_voice_score,
        "mention_count": count,
        "evidence": evidence,
    }


def _score_topic_affinity(public_voice_evidence: list, criteria: dict) -> dict:
    """Haiku pass to extract topics from public commentary, score against champion_topic_signals."""
    if not public_voice_evidence or not anthropic or not os.environ.get("ANTHROPIC_API_KEY"):
        return {"topic_affinity_score": 0.0, "matched_topics": [], "evidence": []}

    topic_signals = criteria.get("champion_topic_signals", [])
    if not topic_signals:
        return {"topic_affinity_score": 0.0, "matched_topics": [], "evidence": []}

    # Build text from public voice evidence
    text_parts = []
    for ev in public_voice_evidence:
        text_parts.append(f"Title: {ev.get('title', '')}\nSnippet: {ev.get('snippet', '')}")

    combined = "\n---\n".join(text_parts)

    system = (
        "You extract topics from text snippets. Output ONLY a JSON array of strings "
        "representing the topics discussed. No markdown, no explanation. "
        "Focus on: technology, security, infrastructure, management, cloud, compliance topics."
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1000,
            system=system,
            messages=[{"role": "user", "content": f"Extract topics from:\n\n{combined}"}],
        )
        text = response.content[0].text.strip()
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)
        topics = json.loads(text)
    except Exception:
        topics = []

    # Score topic overlap
    topics_lower = [t.lower() for t in topics]
    matched = []
    for signal in topic_signals:
        signal_lower = signal.lower()
        for topic in topics_lower:
            if signal_lower in topic or topic in signal_lower:
                matched.append(signal)
                break

    # Score: 0.20 max
    if len(matched) >= 3:
        score = 0.20
    elif len(matched) >= 2:
        score = 0.15
    elif len(matched) >= 1:
        score = 0.10
    else:
        score = 0.0

    return {
        "topic_affinity_score": score,
        "matched_topics": matched,
        "extracted_topics": topics[:10],
        "evidence": public_voice_evidence[:3],
    }


def _score_role_fit(title: str, criteria: dict) -> dict:
    """Score role fit from title parsing against role_priority mapping."""
    role_priority = criteria.get("role_priority", {})
    title_lower = title.lower()

    best_match = None
    best_score = 0.0

    for role, priority in role_priority.items():
        role_words = role.lower().replace("_", " ")
        if role_words in title_lower or any(
            w in title_lower for w in role_words.split() if len(w) > 3
        ):
            if priority > best_score:
                best_score = priority
                best_match = role

    # Scale to weight (0.25 max)
    role_fit_score = best_score * 0.25

    return {
        "role_fit_score": role_fit_score,
        "matched_role": best_match,
        "raw_priority": best_score,
    }


def _score_authority(title: str, recent_activity: list) -> dict:
    """Score authority signals from title level and budget mentions."""
    title_lower = title.lower()
    score = 0.0

    # Director-level or above
    senior_kw = ["chief", "vice president", "vp", "director", "superintendent",
                 "provost", "dean", "president", "cio", "cto", "cfo", "ciso", "cso"]
    if any(kw in title_lower for kw in senior_kw):
        score += 0.10

    # C-level / VP
    c_kw = ["chief", "president", "provost"]
    if any(kw in title_lower for kw in c_kw):
        score += 0.05

    # Budget-related mentions in activity
    for act in (recent_activity or []):
        ctx = (act.get("context") or "").lower()
        if any(w in ctx for w in ["budget", "funding", "invest", "million", "procurement"]):
            score += 0.05
            break

    return {
        "authority_score": min(score, 0.15),
        "is_director_plus": score >= 0.10,
    }


# ---------------------------------------------------------------------------
# Aggregate scoring
# ---------------------------------------------------------------------------

def _compute_weighted_score(signals: dict, criteria: dict) -> float:
    """Compute weighted champion_fit_score from individual signals."""
    weights = criteria.get("weights", {
        "role_fit": 0.25,
        "recency": 0.10,
        "career_arc": 0.15,
        "public_voice": 0.15,
        "topic_affinity": 0.20,
        "authority": 0.15,
    })

    raw_scores = {
        "role_fit": signals.get("role_fit", {}).get("role_fit_score", 0.0),
        "recency": signals.get("tenure", {}).get("recency_score", 0.0),
        "career_arc": signals.get("career_arc", {}).get("career_arc_score", 0.0),
        "public_voice": signals.get("public_voice", {}).get("public_voice_score", 0.0),
        "topic_affinity": signals.get("topic_affinity", {}).get("topic_affinity_score", 0.0),
        "authority": signals.get("authority", {}).get("authority_score", 0.0),
    }

    # Normalize each raw score to 0-1 range (they're already pre-scaled to
    # their max weight, so divide by weight to get 0-1)
    normalized = {}
    for factor, raw in raw_scores.items():
        w = weights.get(factor, 0.15)
        normalized[factor] = min(raw / w, 1.0) if w > 0 else 0.0

    # Weighted sum
    total = sum(normalized[f] * weights.get(f, 0.15) for f in normalized)
    return round(min(total, 1.0), 2)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _cache_path(company_name: str) -> Path:
    slug = _slugify(company_name)
    return SOURCES_DIR / slug / "champion_signals.json"


def _cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        retrieved = data.get("retrieved_at", "")
        if not retrieved:
            return False
        from datetime import timedelta
        ts = datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - ts
        return age < timedelta(days=CACHE_TTL_DAYS)
    except (json.JSONDecodeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------

def fetch_champion_signals(
    company_name: str,
    force_refresh: bool = False,
    max_candidates: int = 5,
) -> dict:
    """Gather champion signals for named individuals from leadership.json.

    Reads leadership.json to get named individuals, then runs Tavily searches
    and Haiku topic extraction per individual. Outputs scored champion signals.
    """
    slug = _slugify(company_name)
    cache = _cache_path(company_name)

    if not force_refresh and _cache_is_fresh(cache):
        try:
            return json.loads(cache.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Read leadership.json
    leadership_path = SOURCES_DIR / slug / "leadership.json"
    if not leadership_path.exists():
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "champion_signals",
            "company": company_name,
            "status": "insufficient_data",
            "reason": "leadership.json not found — run leadership client first",
            "individuals": [],
        }
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(result, indent=2, default=str))
        return result

    try:
        leadership = json.loads(leadership_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"status": "insufficient_data", "reason": "leadership.json unreadable",
                "individuals": []}

    individuals = leadership.get("named_individuals", [])
    if not individuals:
        result = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source": "champion_signals",
            "company": company_name,
            "status": "insufficient_data",
            "reason": "No named individuals in leadership.json",
            "individuals": [],
        }
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(result, indent=2, default=str))
        return result

    # Load champion criteria from persona
    criteria = _load_champion_criteria()

    # Score each individual (cap at max_candidates to conserve API calls)
    scored = []
    for ind in individuals[:max_candidates]:
        name = ind.get("name", "")
        title = ind.get("title", "")
        if not name:
            continue

        print(f"  [champion] scoring {name}...", file=sys.stderr, flush=True)

        # Gather signals
        signals = {}

        # Role fit (no API calls needed)
        signals["role_fit"] = _score_role_fit(title, criteria)

        # Authority (no API calls needed)
        signals["authority"] = _score_authority(title, ind.get("recent_activity", []))

        # Tenure / recency (1 Tavily search)
        if TAVILY_API_KEY:
            signals["tenure"] = _gather_tenure_signal(name, company_name)
        else:
            signals["tenure"] = {"recency_score": 0.0, "evidence": []}

        # Career arc (1 Tavily search)
        if TAVILY_API_KEY:
            signals["career_arc"] = _gather_career_arc(name, company_name, criteria)
        else:
            signals["career_arc"] = {"career_arc_score": 0.0, "prior_employers_detected": [],
                                     "evidence": []}

        # Public voice (3 Tavily searches)
        if TAVILY_API_KEY:
            signals["public_voice"] = _gather_public_voice(name, company_name)
        else:
            signals["public_voice"] = {"public_voice_score": 0.0, "mention_count": 0,
                                       "evidence": []}

        # Topic affinity (Haiku pass on public voice evidence)
        signals["topic_affinity"] = _score_topic_affinity(
            signals["public_voice"].get("evidence", []), criteria
        )

        # Compute weighted score
        champion_fit_score = _compute_weighted_score(signals, criteria)

        scored.append({
            "name": name,
            "title": title,
            "role_classification": ind.get("role_classification", "Other"),
            "champion_fit_score": champion_fit_score,
            "score_breakdown": {
                "role_fit": round(signals["role_fit"]["role_fit_score"], 3),
                "recency": round(signals["tenure"]["recency_score"], 3),
                "career_arc": round(signals["career_arc"]["career_arc_score"], 3),
                "public_voice": round(signals["public_voice"]["public_voice_score"], 3),
                "topic_affinity": round(signals["topic_affinity"]["topic_affinity_score"], 3),
                "authority": round(signals["authority"]["authority_score"], 3),
            },
            "signals": {
                "role_fit": signals["role_fit"],
                "tenure": signals["tenure"],
                "career_arc": signals["career_arc"],
                "public_voice": signals["public_voice"],
                "topic_affinity": signals["topic_affinity"],
                "authority": signals["authority"],
            },
            "linkedin_search_url": ind.get("linkedin_search_url", ""),
            "source_urls": ind.get("source_urls", []),
        })

    # Sort by score descending
    scored.sort(key=lambda x: x["champion_fit_score"], reverse=True)

    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "champion_signals",
        "company": company_name,
        "status": "ok" if scored else "insufficient_data",
        "criteria_used": {
            "weights": criteria.get("weights", {}),
            "topic_signals": criteria.get("champion_topic_signals", []),
        },
        "individuals": scored,
    }

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, indent=2, default=str))

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python champion_signals.py <company_name>", file=sys.stderr)
        sys.exit(1)

    company = sys.argv[1]
    result = fetch_champion_signals(company, force_refresh=True)
    print(json.dumps(result, indent=2))

    if result.get("individuals"):
        print(f"\nTop 3 candidates:", file=sys.stderr)
        for ind in result["individuals"][:3]:
            print(f"  {ind['champion_fit_score']:.2f}  {ind['name']} — {ind['title']}",
                  file=sys.stderr)
            bd = ind["score_breakdown"]
            print(f"        role={bd['role_fit']:.3f} recency={bd['recency']:.3f} "
                  f"arc={bd['career_arc']:.3f} voice={bd['public_voice']:.3f} "
                  f"topic={bd['topic_affinity']:.3f} auth={bd['authority']:.3f}",
                  file=sys.stderr)
