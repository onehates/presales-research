#!/usr/bin/env python3
"""
Orchestrator — /research pipeline entry point.

Phase 0: Detect vertical (name heuristic → SEC SIC → Haiku LLM → web search + Haiku).
Phase 1: Run source clients in parallel, filtered by detected vertical.
Phase 2: Run 3 subagents (company-bg, tech-and-pain, hiring-signals) in parallel.
Phase 3: Run synthesizer (Opus) reading all 3 subagent outputs + persona.
Phase 4: Render brief JSON → HTML via render/render.py.

Usage:
    python orchestrator.py "Atlanta Public Schools" [--force] [--open] [--no-cache]
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    import anthropic
except ImportError:
    anthropic = None

# Debug: confirm API key is available in this process
_key = os.environ.get("ANTHROPIC_API_KEY", "")
print(f"[orchestrator] ANTHROPIC_API_KEY: {_key[:15] + '...' if _key else 'NOT SET'}", flush=True)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
SOURCES_DIR = PROJECT_ROOT / "sources"
BRIEFS_DIR = PROJECT_ROOT / "briefs"
PERSONA_PATH = PROJECT_ROOT / "persona" / "verkada-se.yml"
AGENTS_DIR = PROJECT_ROOT / ".claude" / "agents"
RENDER_SCRIPT = PROJECT_ROOT / "render" / "render.py"

SONNET_MODEL = "claude-sonnet-4-6"
OPUS_MODEL = "claude-opus-4-6"

# Prompt caching beta header
PROMPT_CACHE_HEADERS = {"anthropic-beta": "prompt-caching-2024-07-31"}

# Subagent cache TTL (24 hours)
SUBAGENT_CACHE_TTL = 86400

# ---------------------------------------------------------------------------
# Canonical vertical enum — derived from persona/verkada-se.yml at startup
# ---------------------------------------------------------------------------

# Mapping from persona file vertical names → code-level slugs
_PERSONA_VERTICAL_SLUGS = {
    "K-12": "k12",
    "Healthcare": "healthcare",
    "Retail": "retail",
    "Manufacturing": "manufacturing",
    "HigherEd": "higher_ed",
    "CRE_Hospitality": "hospitality",
}

# Verticals the orchestrator supports that aren't (yet) in the persona file
# but have distinct source-routing and keyword detection needs.
# When a new vertical is added to persona/verkada-se.yml, add a slug mapping
# above and remove it from this list.
_EXTRA_VERTICALS = [
    "senior_living", "state_local_gov", "federal",
    "public_safety", "transportation", "critical_infrastructure",
]


def _load_verticals_from_persona() -> list[str]:
    """Parse icp.verticals from the persona YAML and return slug list.

    Falls back to the slug mapping keys if the file can't be read.
    Always appends _EXTRA_VERTICALS and 'unknown'.
    """
    slugs = []
    try:
        import yaml
        data = yaml.safe_load(PERSONA_PATH.read_text())
        for v in data.get("icp", {}).get("verticals", []):
            name = v.get("name", "")
            slug = _PERSONA_VERTICAL_SLUGS.get(name)
            if slug:
                slugs.append(slug)
            else:
                # Auto-slugify unknown persona verticals
                auto = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
                if auto:
                    slugs.append(auto)
    except Exception:
        # Fallback: use the static mapping
        slugs = list(_PERSONA_VERTICAL_SLUGS.values())
    slugs.extend(v for v in _EXTRA_VERTICALS if v not in slugs)
    slugs.append("unknown")
    return slugs


VALID_VERTICALS = _load_verticals_from_persona()

# SIC code prefix → vertical mapping (SEC uses SIC, not NAICS)
# Used by Phase 0 SEC quick lookup
SIC_TO_VERTICAL = {
    # Healthcare
    "80": "healthcare",   # Health Services
    "8011": "healthcare", # Offices of physicians
    "8021": "healthcare", # Offices of dentists
    "8041": "healthcare", # Offices of chiropractors
    "8042": "healthcare", # Offices of optometrists
    "8049": "healthcare", # Other health practitioners
    "8051": "senior_living",  # Skilled nursing facilities
    "8052": "senior_living",  # Intermediate care facilities
    "8059": "senior_living",  # Nursing/personal care
    "806": "healthcare",  # Hospitals
    "807": "healthcare",  # Medical/dental labs
    "808": "healthcare",  # Home health care
    "809": "healthcare",  # Health services NEC
    # Retail
    "52": "retail",       # Building materials/garden
    "53": "retail",       # General merchandise
    "54": "retail",       # Food stores
    "55": "retail",       # Auto dealers/gas stations
    "56": "retail",       # Apparel/accessory stores
    "57": "retail",       # Home furniture/furnishings
    "58": "hospitality",  # Eating/drinking places
    "59": "retail",       # Retail stores NEC
    # Manufacturing
    "20": "manufacturing", "21": "manufacturing", "22": "manufacturing",
    "23": "manufacturing", "24": "manufacturing", "25": "manufacturing",
    "26": "manufacturing", "27": "manufacturing", "28": "manufacturing",
    "29": "manufacturing", "30": "manufacturing", "31": "manufacturing",
    "32": "manufacturing", "33": "manufacturing", "34": "manufacturing",
    "35": "manufacturing", "36": "manufacturing", "37": "manufacturing",
    "38": "manufacturing", "39": "manufacturing",
    # Transportation
    "40": "transportation", "41": "transportation", "42": "transportation",
    "43": "transportation", "44": "transportation", "45": "transportation",
    "46": "transportation", "47": "transportation",
    # Hospitality
    "70": "hospitality",  # Hotels/lodging
    "701": "hospitality", # Hotels and motels
    "7011": "hospitality",
    # Education
    "82": "higher_ed",    # Educational services
    "8211": "k12",        # Elementary/secondary schools
    "8221": "higher_ed",  # Colleges/universities
    # Critical infrastructure
    "49": "critical_infrastructure",  # Electric/gas/sanitary
    "48": "critical_infrastructure",  # Communications
    # Government-adjacent (public admin)
    "91": "state_local_gov", "92": "public_safety", "93": "state_local_gov",
    "94": "state_local_gov", "95": "state_local_gov", "96": "federal",
    "97": "federal",
}

# Token usage accumulator (populated by _stream_anthropic_with_retry)
_token_usage = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}

# Status file for live dashboard
_status_path: Path | None = None

# Pricing per 1M tokens (Opus 4.6 / Sonnet 4.6)
_PRICING = {
    OPUS_MODEL: {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    SONNET_MODEL: {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
}


def _estimate_cost() -> float:
    """Estimate total cost from accumulated token usage using blended rates."""
    # Use Opus rates as conservative estimate (synthesizer dominates cost)
    p = _PRICING[OPUS_MODEL]
    return (
        _token_usage["input"] * p["input"]
        + _token_usage["output"] * p["output"]
        + _token_usage["cache_read"] * p["cache_read"]
        + _token_usage["cache_creation"] * p["cache_write"]
    ) / 1_000_000


import threading
_status_lock = threading.Lock()


def _write_status(phase: str, message: str, **fields):
    """Append a status event to /tmp/orchestrator-status-{slug}.json.

    The file contains {slug, started_at, current_phase, events: [...]}.
    Each event has {phase, message, timestamp, **fields}.
    Thread-safe for Phase 1 parallel source collection.
    """
    if _status_path is None:
        return
    event = {
        "phase": phase,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    with _status_lock:
        try:
            if _status_path.exists():
                status = json.loads(_status_path.read_text())
            else:
                status = {"events": []}
        except (json.JSONDecodeError, OSError):
            status = {"events": []}
        status["current_phase"] = phase
        status["current_message"] = message
        # Always update token/cost fields
        status["tokens_input"] = _token_usage["input"]
        status["tokens_output"] = _token_usage["output"]
        status["cache_read"] = _token_usage["cache_read"]
        status["cache_created"] = _token_usage["cache_creation"]
        status["cost_estimate"] = round(_estimate_cost(), 4)
        status.update(fields)
        status["events"].append(event)
        _status_path.write_text(json.dumps(status, indent=2, default=str))

# ---------------------------------------------------------------------------
# Client registry — maps client name to (module_path, fetch_function, args_fn)
# args_fn(company, slug) → dict of kwargs beyond company_name/force_refresh
# ---------------------------------------------------------------------------

def _company_args(company, slug):
    return {"company_name": company}

def _entity_args(company, slug):
    return {"entity_name": company}

def _query_args(company, slug):
    return {"query": company}

PROCUREMENT_KEYWORDS = [
    "video surveillance",
    "access control",
    "physical security",
    "school safety",
]


def _category_args(company, slug):
    return {"query": "video surveillance"}

def _nces_args(company, slug):
    return {"district_name": company, "state": "GA"}

def _clery_args(company, slug):
    return {"institution_name": company, "state": "GA"}

def _sam_args(company, slug):
    return {"entity_name": company, "state": "GA"}

def _crtsh_args(company, slug):
    return {"company_name": company}


def _champion_signals_args(company, slug):
    """Champion signals depends on leadership.json existing."""
    return {"company_name": company}


def _leadership_args(company, slug):
    """Detect entity_type from cached NCES/SEC data for leadership client."""
    entity_type = "unknown"
    nces_path = SOURCES_DIR / slug / "nces.json"
    if nces_path.exists():
        try:
            nces = json.loads(nces_path.read_text())
            if nces.get("district_metadata"):
                entity_type = "k12"
        except (json.JSONDecodeError, OSError):
            pass
    if entity_type == "unknown":
        clery_path = SOURCES_DIR / slug / "clery.json"
        if clery_path.exists():
            try:
                clery = json.loads(clery_path.read_text())
                if clery.get("status") != "insufficient_data":
                    entity_type = "higher_ed"
            except (json.JSONDecodeError, OSError):
                pass
    if entity_type == "unknown":
        sec_path = SOURCES_DIR / slug / "sec.json"
        if sec_path.exists():
            try:
                sec = json.loads(sec_path.read_text())
                if sec.get("status") != "insufficient_data" and sec.get("company"):
                    # Name heuristics may identify a more specific vertical
                    name_vert = detect_vertical_from_name(company)
                    entity_type = name_vert or "unknown"
            except (json.JSONDecodeError, OSError):
                pass
    return {"company_name": company, "entity_type": _validate_vertical(entity_type)}


CLIENT_REGISTRY = {
    # (module_name, fetch_fn_name, args_factory, cache_filename)
    "sec":                  ("clients.sec",               "fetch_sec_data",                _company_args,  "sec.json"),
    "indeed":               ("clients.indeed",            "fetch_jobs_data",               _company_args,  "jobs.json"),
    "crtsh":                ("clients.crtsh",             "fetch_crtsh_data",              _crtsh_args,    "ssl.json"),
    "github":               ("clients.github",            "fetch_github_data",             _company_args,  "github.json"),
    "news":                 ("clients.news",              "fetch_news_data",               _company_args,  "news.json"),
    "website":              ("clients.website",           "fetch_website_data",            _company_args,  "website.json"),
    "nces":                 ("clients.nces",              "fetch_nces_data",               _nces_args,     "nces.json"),
    "clery":                ("clients.clery",             "fetch_clery_data",              _clery_args,    "clery.json"),
    "sam":                  ("clients.sam",               "fetch_sam_data",                _sam_args,      "sam.json"),
    "sourcewell":           ("clients.sourcewell",        "fetch_sourcewell_data",         _category_args, "sourcewell.json"),
    "tips":                 ("clients.tips",              "fetch_tips_data",               _category_args, "tips.json"),
    "hhs":                  ("clients.hhs",               "fetch_hhs_data",                _entity_args,   "hhs.json"),
    "reddit":               ("clients.reddit",            "fetch_reddit_data",             _company_args,  "reddit.json"),
    "ga_procurement":       ("clients.ga_procurement",    "fetch_ga_procurement_data",     _category_args, "ga_procurement.json"),
    "atlanta_procurement":  ("clients.atlanta_procurement","fetch_atlanta_procurement_data",_category_args, "atlanta_procurement.json"),
    "sled_procurement":     ("clients.sled_procurement",  "fetch_sled_procurement_data",   _company_args,  "sled_procurement.json"),
    "omnia":                ("clients.omnia",             "fetch_omnia_data",              _category_args, "omnia.json"),
    "costars":              ("clients.costars",           "fetch_costars_data",            _category_args, "costars.json"),
    "hgac":                 ("clients.hgac",              "fetch_hgac_data",               _category_args, "hgac.json"),
    "leadership":           ("clients.leadership",        "fetch_leadership_data",         _leadership_args,  "leadership.json"),
    "champion_signals":     ("clients.champion_signals",  "fetch_champion_signals",        _champion_signals_args, "champion_signals.json"),
}

# Sources that work for ANY entity and ALWAYS run regardless of vertical
UNIVERSAL_SOURCES = {
    "sec", "github", "news", "indeed", "reddit", "crtsh",
    "leadership", "champion_signals", "website",
}


def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


# ---------------------------------------------------------------------------
# Phase 1 — Source data collection
# ---------------------------------------------------------------------------

# Status symbols
SYM_OK = "\033[32m✓\033[0m"       # green checkmark
SYM_CACHED = "\033[36m✓\033[0m"   # cyan checkmark (cached)
SYM_INSUF = "\033[33m—\033[0m"    # yellow dash
SYM_ERROR = "\033[31m✗\033[0m"    # red X
SYM_RUN = "\033[90m…\033[0m"      # gray dots (running)
SYM_SKIP = "\033[90m⊘\033[0m"    # gray circle-slash (skipped)


COOPERATIVE_CLIENTS = {"sourcewell", "tips", "ga_procurement", "atlanta_procurement", "omnia", "costars", "hgac"}

# Clients that depend on other clients' output and must run after Phase 1
DEFERRED_CLIENTS = {"champion_signals"}

# ---------------------------------------------------------------------------
# Vertical-aware source filtering
# ---------------------------------------------------------------------------

SOURCES_BY_VERTICAL = {
    "k12": {"github", "news", "nces", "clery", "sam", "sourcewell", "tips", "hhs", "reddit", "ga_procurement", "atlanta_procurement", "sled_procurement", "omnia", "costars", "hgac", "leadership", "champion_signals", "crtsh", "indeed"},
    "higher_ed": {"sec", "github", "news", "clery", "sam", "sourcewell", "reddit", "sled_procurement", "omnia", "leadership", "champion_signals", "crtsh", "indeed"},
    "healthcare": {"sec", "github", "news", "hhs", "sam", "reddit", "leadership", "champion_signals", "crtsh", "indeed"},
    "state_local_gov": {"github", "news", "sam", "sourcewell", "ga_procurement", "atlanta_procurement", "sled_procurement", "omnia", "leadership", "champion_signals", "crtsh", "indeed"},
    "federal": {"github", "news", "sam", "leadership", "champion_signals", "crtsh", "indeed"},
    "retail": {"sec", "github", "news", "reddit", "leadership", "champion_signals", "crtsh", "indeed", "hhs"},
    "manufacturing": {"sec", "github", "news", "reddit", "leadership", "champion_signals", "crtsh", "indeed"},
    "hospitality": {"sec", "github", "news", "reddit", "leadership", "champion_signals", "crtsh", "indeed"},
    "critical_infrastructure": {"sec", "github", "news", "sam", "reddit", "leadership", "champion_signals", "crtsh", "indeed"},
    "transportation": {"sec", "github", "news", "sam", "reddit", "leadership", "champion_signals", "crtsh", "indeed"},
    "public_safety": {"github", "news", "sam", "sourcewell", "ga_procurement", "omnia", "leadership", "champion_signals", "crtsh", "indeed"},
    "senior_living": {"sec", "github", "news", "hhs", "reddit", "leadership", "champion_signals", "crtsh", "indeed"},
}

# Name-based heuristics for early vertical detection (before any sources run)
# Order matters: check most specific first (public_safety before state_local_gov,
# healthcare before state_local_gov, etc.)
_HEALTHCARE_KEYWORDS = [
    "hospital", "medical center", "health system", "clinic",
    "memorial hospital", "regional medical", "healthcare", "health care",
    "ambulatory",
]
_K12_KEYWORDS = [
    "public schools", "school district", "isd", "unified school",
    "school dept", "schools", "independent school district",
    "county schools", "parish schools", "city schools", "school board",
]
_HIGHER_ED_KEYWORDS = [
    "university", "college", "institute of technology",
    "polytechnic", "community college", "u of", "state university",
]
_SENIOR_KEYWORDS = [
    "senior living", "assisted living", "nursing home",
    "memory care", "retirement community", "elder care",
]
_PUBLIC_SAFETY_KEYWORDS = [
    "police department", "sheriff", "fire department",
    "fire & rescue", "fire dept", "police dept",
]
_TRANSPORTATION_KEYWORDS = [
    "airport", "airlines", "transit authority", "port of",
    "marta", "rail",
]
_HOSPITALITY_KEYWORDS = [
    "hotel", "resort", "marriott", "hilton", "hyatt", "ihg",
    "wyndham", "casino", "resorts",
]
_STATE_LOCAL_GOV_KEYWORDS = [
    "city of", "county of", "department of", "state of",
    "bureau of", "agency", "commission", "authority",
]

# Ordered list: (keywords, vertical) — most specific first
_KEYWORD_VERTICAL_MAP = [
    (_HEALTHCARE_KEYWORDS, "healthcare"),
    (_PUBLIC_SAFETY_KEYWORDS, "public_safety"),
    (_TRANSPORTATION_KEYWORDS, "transportation"),
    (_K12_KEYWORDS, "k12"),
    (_HIGHER_ED_KEYWORDS, "higher_ed"),
    (_SENIOR_KEYWORDS, "senior_living"),
    (_HOSPITALITY_KEYWORDS, "hospitality"),
    (_STATE_LOCAL_GOV_KEYWORDS, "state_local_gov"),
]


def _validate_vertical(vertical: str) -> str:
    """Ensure vertical is one of VALID_VERTICALS. Returns 'unknown' with warning if not."""
    if vertical in VALID_VERTICALS:
        return vertical
    print(f"    \033[33m⚠\033[0m  Invalid vertical '{vertical}' — forcing 'unknown'", flush=True)
    return "unknown"


def detect_vertical_from_name(company: str) -> str | None:
    """Detect vertical from company name keywords only.

    Returns a VALID_VERTICALS string or None if name is ambiguous.
    Exported for testing.
    """
    name_lower = company.lower()
    for keywords, vertical in _KEYWORD_VERTICAL_MAP:
        for kw in keywords:
            if kw in name_lower:
                return vertical
    return None


def detect_vertical_early(company: str, slug: str) -> str:
    """Guess vertical from company name heuristics and any pre-existing cached data.

    Returns a VALID_VERTICALS string or 'unknown'. All paths are validated.
    """
    name_lower = company.lower()

    # Check cached data first (from prior runs)
    nces_path = SOURCES_DIR / slug / "nces.json"
    if nces_path.exists():
        try:
            nces = json.loads(nces_path.read_text())
            if nces.get("district_metadata"):
                return _validate_vertical("k12")
        except Exception:
            pass

    clery_path = SOURCES_DIR / slug / "clery.json"
    if clery_path.exists():
        try:
            clery = json.loads(clery_path.read_text())
            if clery.get("status") != "insufficient_data":
                return _validate_vertical("higher_ed")
        except Exception:
            pass

    # SEC data indicates public corporation — but name heuristics may override
    # (e.g., "HCA Healthcare" is SEC-listed but vertical is healthcare, not generic)
    sec_detected = False
    sec_path = SOURCES_DIR / slug / "sec.json"
    if sec_path.exists():
        try:
            sec = json.loads(sec_path.read_text())
            if sec.get("status") != "insufficient_data" and sec.get("company"):
                sec_detected = True
        except Exception:
            pass

    # Name-based heuristics (ordered by specificity)
    name_vertical = detect_vertical_from_name(company)
    if name_vertical:
        return _validate_vertical(name_vertical)

    # SEC fallback — it's a public company but name didn't reveal vertical
    if sec_detected:
        return _validate_vertical("unknown")

    return "unknown"


HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _haiku_classify_vertical(company_name: str, context: str = "") -> str:
    """Use Haiku to classify company into one of VALID_VERTICALS.

    Cost: ~$0.001 per call. Returns validated vertical or 'unknown'.
    """
    if not anthropic or not os.environ.get("ANTHROPIC_API_KEY"):
        return "unknown"

    valid_list = ", ".join(v for v in VALID_VERTICALS if v != "unknown")
    prompt = (
        f"Classify this company into ONE vertical from this exact list:\n"
        f"{valid_list}\n\n"
        f"Company: {company_name}\n"
        f"{f'Context: {context[:1500]}' if context else ''}\n\n"
        f"Output JUST the vertical string. If genuinely unclear, output: unknown"
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=30,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text.strip().lower().replace(" ", "_")
        _token_usage["input"] += response.usage.input_tokens
        _token_usage["output"] += response.usage.output_tokens
        return _validate_vertical(result)
    except Exception as e:
        print(f"    \033[33m⚠\033[0m  Haiku classification failed: {str(e)[:60]}", flush=True)
        return "unknown"


def _quick_tavily_search(query: str, max_results: int = 2) -> str:
    """Run a quick Tavily search and return snippet text. Returns '' on failure."""
    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    if not tavily_key:
        return ""
    try:
        import requests
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": tavily_key,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return ""
        results = resp.json().get("results", [])
        snippets = [r.get("content", "")[:300] for r in results if r.get("content")]
        return " | ".join(snippets)
    except Exception:
        return ""


def _resolve_sic_to_vertical(sic: str) -> str | None:
    """Map an SEC SIC code to a vertical using prefix matching (longest first)."""
    if not sic:
        return None
    for prefix_len in [4, 3, 2]:
        prefix = sic[:prefix_len]
        if prefix in SIC_TO_VERTICAL:
            return SIC_TO_VERTICAL[prefix]
    return None


def phase0_detect_vertical(company: str, slug: str) -> tuple[str, str]:
    """Detect vertical BEFORE source collection (Phase 0).

    4-step cascade, stops at first confident result:
      1. Name heuristic (instant, free)
      2. SEC CIK + SIC lookup (fast, free, deterministic)
      3. Haiku LLM classification — name only (fast, ~$0.001)
      4. Web search + Haiku (slower, ~$0.02)

    Returns: (vertical, detection_method)
    """
    print(f"\n  Phase 0 — Vertical Detection")
    print(f"  {'─' * 50}")

    # Step 1: Name heuristic
    name_vertical = detect_vertical_from_name(company)
    if name_vertical and name_vertical != "unknown":
        print(f"    {SYM_OK} {name_vertical} (name heuristic)", flush=True)
        return _validate_vertical(name_vertical), "name_heuristic"

    # Step 2: SEC CIK + SIC lookup (fast — single API call)
    print(f"    {SYM_RUN} checking SEC EDGAR for SIC code...", flush=True)
    try:
        from clients.sec import quick_naics_lookup
        sic_result = quick_naics_lookup(company, slug)
        if sic_result and sic_result.get("sic"):
            sic = sic_result["sic"]
            vertical = _resolve_sic_to_vertical(sic)
            if vertical:
                sic_desc = sic_result.get("sic_description", "")
                print(f"    {SYM_OK} {vertical} (SIC {sic} — {sic_desc})", flush=True)
                return _validate_vertical(vertical), "sec_sic"
            else:
                print(f"    {SYM_INSUF} SIC {sic} ({sic_result.get('sic_description', '')}) — no vertical mapping", flush=True)
        else:
            print(f"    {SYM_INSUF} not found in SEC EDGAR", flush=True)
    except Exception as e:
        print(f"    {SYM_ERROR} SEC lookup failed: {str(e)[:60]}", flush=True)

    # Step 3: Haiku LLM classification (just name)
    if anthropic and os.environ.get("ANTHROPIC_API_KEY"):
        print(f"    {SYM_RUN} asking Haiku to classify based on name...", flush=True)
        haiku_result = _haiku_classify_vertical(company, context="")
        if haiku_result and haiku_result != "unknown":
            print(f"    {SYM_OK} {haiku_result} (Haiku classification)", flush=True)
            return _validate_vertical(haiku_result), "haiku_name"
        else:
            print(f"    {SYM_INSUF} Haiku uncertain from name alone", flush=True)

        # Step 4: Web search + Haiku
        print(f"    {SYM_RUN} running web search for industry context...", flush=True)
        search_snippet = _quick_tavily_search(f"{company} industry classification")
        if search_snippet:
            haiku_result = _haiku_classify_vertical(company, context=search_snippet)
            if haiku_result and haiku_result != "unknown":
                print(f"    {SYM_OK} {haiku_result} (Haiku + web search)", flush=True)
                return _validate_vertical(haiku_result), "haiku_web_search"

    print(f"    {SYM_INSUF} unknown (all methods inconclusive)", flush=True)
    return "unknown", "fallback"


def llm_classify_vertical(company_name: str, slug: str) -> str:
    """Post-Phase-1 LLM vertical classification using cached source data.

    Called as a secondary fallback when Phase 0 returned 'unknown' but
    Phase 1 data (website, news, SEC) may now provide enough context.
    """
    sources_dir = SOURCES_DIR / slug
    context_parts = []

    website_path = sources_dir / "website.json"
    if website_path.exists():
        try:
            data = json.loads(website_path.read_text())
            if data.get("status") == "ok":
                pages = data.get("pages", {})
                for url, text in pages.items():
                    context_parts.append(f"Website ({url}):\n{text[:1500]}")
                    break
        except Exception:
            pass

    news_path = sources_dir / "news.json"
    if news_path.exists():
        try:
            data = json.loads(news_path.read_text())
            articles = data.get("articles", [])
            if articles:
                headlines = [a.get("title", "") for a in articles[:5]]
                context_parts.append(f"Recent news: {'; '.join(headlines)}")
        except Exception:
            pass

    sec_path = sources_dir / "sec.json"
    if sec_path.exists():
        try:
            data = json.loads(sec_path.read_text())
            if data.get("company"):
                sic = data["company"].get("sic_description", "")
                context_parts.append(f"SEC SIC: {sic}")
        except Exception:
            pass

    if not context_parts:
        return "unknown"

    return _haiku_classify_vertical(company_name, context="\n".join(context_parts))


def filter_sources_by_vertical(client_names: list[str], vertical: str) -> tuple[list[str], list[str]]:
    """Filter source client list by detected vertical.

    Returns (applicable_clients, skipped_clients).
    UNIVERSAL_SOURCES always run regardless of vertical.
    If vertical is unknown, all clients run.
    """
    applicable_set = SOURCES_BY_VERTICAL.get(vertical)
    if applicable_set is None:
        return client_names, []
    # Merge vertical-specific sources with universal sources
    combined = applicable_set | UNIVERSAL_SOURCES
    applicable = [n for n in client_names if n in combined]
    skipped = [n for n in client_names if n not in combined]
    return applicable, skipped


def run_client(client_name: str, company: str, slug: str, force: bool) -> tuple[str, str, str]:
    """
    Run a single client. Returns (client_name, status_symbol, detail).

    For cooperative purchasing clients (sourcewell, tips, ga_procurement,
    atlanta_procurement), runs multiple keyword queries and writes results to
    both the keyword-specific cache AND sources/_market/ for cross-company reuse.
    Also copies results into sources/{company}/ so subagents can read them.
    """
    mod_name, fn_name, args_factory, cache_file = CLIENT_REGISTRY[client_name]

    try:
        import importlib
        mod = importlib.import_module(mod_name)
        fetch_fn = getattr(mod, fn_name)

        # Cooperative purchasing: run multiple keywords, aggregate results
        if client_name in COOPERATIVE_CLIENTS:
            return _run_cooperative_client(
                client_name, fetch_fn, slug, cache_file, force
            )

        # Standard client: single call
        cache_path = SOURCES_DIR / slug / cache_file
        was_cached = cache_path.exists() and not force

        kwargs = args_factory(company, slug)
        kwargs["force_refresh"] = force

        result = fetch_fn(**kwargs)

        status = result.get("status", "")
        if status in ("insufficient_data", "no_matches"):
            return client_name, SYM_INSUF, status
        elif was_cached and not force:
            return client_name, SYM_CACHED, "cached"
        else:
            return client_name, SYM_OK, "ok"

    except Exception as e:
        return client_name, SYM_ERROR, str(e)[:80]


def _run_cooperative_client(
    client_name: str, fetch_fn, slug: str, cache_file: str, force: bool
) -> tuple[str, str, str]:
    """Run a cooperative purchasing client with multiple keywords.

    Writes to:
    - sources/_market/{client_name}-{keyword_slug}.json (shared cache)
    - sources/{slug}/{cache_file} (company-specific, aggregated)
    """
    market_dir = SOURCES_DIR / "_market"
    market_dir.mkdir(parents=True, exist_ok=True)
    company_dir = SOURCES_DIR / slug
    company_dir.mkdir(parents=True, exist_ok=True)

    all_results = []
    any_ok = False

    for keyword in PROCUREMENT_KEYWORDS:
        kw_slug = slugify(keyword)
        market_path = market_dir / f"{client_name}-{kw_slug}.json"

        # Check market cache
        if market_path.exists() and not force:
            try:
                cached = json.loads(market_path.read_text())
                all_results.append(cached)
                any_ok = True
                continue
            except (json.JSONDecodeError, KeyError):
                pass

        try:
            result = fetch_fn(query=keyword, force_refresh=force)
            status = result.get("status", "")
            if status not in ("insufficient_data", "no_matches"):
                any_ok = True

            # Write to market cache
            market_path.write_text(json.dumps(result, indent=2, default=str))
            all_results.append(result)
        except Exception:
            continue

    # Aggregate and write to company sources dir
    aggregated = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": client_name,
        "keywords_queried": PROCUREMENT_KEYWORDS,
        "results_by_keyword": {
            r.get("query", PROCUREMENT_KEYWORDS[i] if i < len(PROCUREMENT_KEYWORDS) else "unknown"): r
            for i, r in enumerate(all_results)
        },
    }
    company_cache = company_dir / cache_file
    company_cache.write_text(json.dumps(aggregated, indent=2, default=str))

    if any_ok:
        return client_name, SYM_OK, f"ok ({len(all_results)} keywords)"
    else:
        return client_name, SYM_INSUF, "insufficient_data"


def run_phase1(company: str, slug: str, force: bool, vertical: str = "unknown") -> dict:
    """Run all clients in parallel (except deferred). Returns {client_name: (symbol, detail)}."""
    results = {}
    all_client_names = [n for n in CLIENT_REGISTRY if n not in DEFERRED_CLIENTS]

    # Filter by vertical
    client_names, skipped = filter_sources_by_vertical(all_client_names, vertical)

    # Print initial grid
    vert_label = f"  vertical: {vertical}" if vertical != "unknown" else ""
    print(f"\n  Phase 1 — Source Data Collection{vert_label}")
    print("  " + "─" * 50)

    # Log skipped sources
    if skipped:
        for name in skipped:
            results[name] = (SYM_SKIP, "not applicable")
            print(f"    {SYM_SKIP} {name:<22} not applicable to {vertical}", flush=True)
            _write_status("phase1", f"{name}: skipped (not applicable to {vertical})", source=name, source_status="skipped")

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(run_client, name, company, slug, force): name
            for name in client_names
        }

        try:
            for future in as_completed(futures, timeout=90):
                name = futures[future]
                try:
                    _, symbol, detail = future.result()
                except Exception as e:
                    symbol, detail = SYM_ERROR, str(e)[:80]
                results[name] = (symbol, detail)
                print(f"    {symbol} {name:<22} {detail}", flush=True)
                sym_map = {SYM_OK: "ok", SYM_CACHED: "cached", SYM_INSUF: "insufficient_data", SYM_ERROR: "error"}
                _write_status("phase1", f"{name}: {detail}", source=name, source_status=sym_map.get(symbol, "unknown"))
        except TimeoutError:
            # Mark any unfinished futures as timed out
            for future, name in futures.items():
                if name not in results:
                    future.cancel()
                    results[name] = (SYM_ERROR, "timeout (>90s)")
                    print(f"    {SYM_ERROR} {name:<22} timeout (>90s)", flush=True)
                    _write_status("phase1", f"{name}: timeout", source=name, source_status="error")

    # Summary line
    ok_count = sum(1 for s, _ in results.values() if s in (SYM_OK, SYM_CACHED))
    insuf_count = sum(1 for s, _ in results.values() if s == SYM_INSUF)
    err_count = sum(1 for s, _ in results.values() if s == SYM_ERROR)
    skip_count = sum(1 for s, _ in results.values() if s == SYM_SKIP)
    summary = f"\n    {ok_count} sourced  {insuf_count} insufficient  {err_count} errored"
    if skip_count:
        summary += f"  {skip_count} skipped (not applicable)"
    print(summary)

    if ok_count < 2:
        print(f"\n  ⚠  WARNING: <2 sources returned data. Brief will be thin.", flush=True)

    return results


# ---------------------------------------------------------------------------
# Phase 2 — Subagent synthesis
# ---------------------------------------------------------------------------

def read_agent_prompt(agent_name: str) -> str:
    """Read the agent .md file, strip frontmatter, return the system prompt."""
    path = AGENTS_DIR / f"{agent_name}.md"
    text = path.read_text()
    # Strip YAML frontmatter
    if text.startswith("---"):
        end = text.index("---", 3)
        text = text[end + 3:].strip()
    return text


SUBAGENT_SOURCE_FILES = {
    "company-bg": ["sec.json", "nces.json", "clery.json", "sam.json", "news.json", "website.json"],
    "tech-and-pain": ["ssl.json", "github.json", "news.json", "reddit.json", "hhs.json"],
    "hiring-signals": ["jobs.json"],
}


def _load_source_data_for_agent(agent_name: str, slug: str) -> str:
    """Load cached source files, return as formatted text for injection.

    Note: persona file is no longer included here — it's injected via the
    cached system prompt in run_subagent() for prompt caching efficiency.
    """
    parts = []
    source_files = SUBAGENT_SOURCE_FILES.get(agent_name, [])
    sources_dir = SOURCES_DIR / slug

    for filename in source_files:
        path = sources_dir / filename
        if path.exists():
            try:
                data = path.read_text()
                # Truncate very large files to avoid token limits
                if len(data) > 50000:
                    data = data[:50000] + "\n... [truncated]"
                parts.append(f"=== sources/{slug}/{filename} ===\n{data}")
            except OSError:
                parts.append(f"=== sources/{slug}/{filename} ===\n[ERROR: could not read file]")
        else:
            parts.append(f"=== sources/{slug}/{filename} ===\n[FILE NOT FOUND]")

    return "\n\n".join(parts)


RETRY_DELAYS = [5, 10, 20, 40, 60]


def _build_cached_system(agent_prompt: str, persona_text: str | None = None) -> list[dict]:
    """Build a system prompt as structured content blocks with cache_control markers.

    The persona file and agent system prompt are marked for ephemeral caching.
    When reused across sequential calls within 5 min, cached reads cost 10% of
    normal input tokens.
    """
    blocks = []
    if persona_text:
        blocks.append({
            "type": "text",
            "text": f"=== persona/verkada-se.yml ===\n{persona_text}",
            "cache_control": {"type": "ephemeral"},
        })
    blocks.append({
        "type": "text",
        "text": agent_prompt,
        "cache_control": {"type": "ephemeral"},
    })
    return blocks


def _stream_anthropic_with_retry(client, *, model: str, max_tokens: int,
                                  system: str | list, messages: list,
                                  label: str, thinking: dict | None = None) -> str:
    """Stream an Anthropic API call with exponential backoff on 429/529.

    Uses client.messages.stream() to avoid the SDK's 10-minute timeout
    on non-streaming calls (large prompts + high max_tokens).
    Returns the accumulated response text.

    system can be a plain string or a list of content blocks (for prompt caching).
    When a list is passed, the prompt-caching beta header is included.

    thinking: optional dict like {"type": "enabled", "budget_tokens": 8000}
    to enable extended thinking (Sonnet 4.6).
    """
    use_cache = isinstance(system, list)
    extra_headers = PROMPT_CACHE_HEADERS if use_cache else None

    call_kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        extra_headers=extra_headers,
    )
    if thinking:
        call_kwargs["thinking"] = thinking

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            with client.messages.stream(**call_kwargs) as stream:
                chunks = []
                for text in stream.text_stream:
                    chunks.append(text)

                # Track token usage from final message
                try:
                    final = stream.get_final_message()
                    usage = final.usage
                    _token_usage["input"] += usage.input_tokens
                    _token_usage["output"] += usage.output_tokens
                    _token_usage["cache_creation"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
                    _token_usage["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
                    # Log cache hits for visibility
                    cache_created = getattr(usage, "cache_creation_input_tokens", 0) or 0
                    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                    if cache_created:
                        print(f"    💾 {label}: cached {cache_created} input tokens", flush=True)
                    if cache_read:
                        print(f"    ⚡ {label}: read {cache_read} cached tokens (90% savings)", flush=True)
                    _write_status(
                        "tokens", f"{label} tokens",
                        tokens_input=_token_usage["input"],
                        tokens_output=_token_usage["output"],
                        cache_read=_token_usage["cache_read"],
                        cache_created=_token_usage["cache_creation"],
                        cost_estimate=round(_estimate_cost(), 4),
                    )
                except Exception:
                    pass

                return "".join(chunks)
        except anthropic.RateLimitError:
            if attempt >= len(RETRY_DELAYS):
                raise
            delay = RETRY_DELAYS[attempt]
            print(f"    ⏳ {label}: rate limited (429), retrying in {delay}s... (attempt {attempt + 2}/{len(RETRY_DELAYS) + 1})",
                  file=sys.stderr, flush=True)
            time.sleep(delay)
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                print(f"    ⏳ {label}: API overloaded (529), retrying in {delay}s... (attempt {attempt + 2}/{len(RETRY_DELAYS) + 1})",
                      file=sys.stderr, flush=True)
                time.sleep(delay)
            else:
                raise


def _source_fingerprint(agent_name: str, slug: str) -> str:
    """Compute a fingerprint hash from the source files a subagent reads.

    Uses mtime + size of each input JSON to detect changes without reading content.
    """
    source_files = SUBAGENT_SOURCE_FILES.get(agent_name, [])
    sources_dir = SOURCES_DIR / slug
    parts = []
    for filename in source_files:
        path = sources_dir / filename
        if path.exists():
            stat = path.stat()
            parts.append(f"{filename}:{stat.st_mtime_ns}:{stat.st_size}")
        else:
            parts.append(f"{filename}:missing")
    # Include persona file in fingerprint (triggers/templates may change)
    if PERSONA_PATH.exists():
        stat = PERSONA_PATH.stat()
        parts.append(f"persona:{stat.st_mtime_ns}:{stat.st_size}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _check_subagent_cache(agent_name: str, slug: str) -> dict | None:
    """Check for a cached subagent output matching the current source fingerprint.

    Returns the cached result dict if found and fresh (< SUBAGENT_CACHE_TTL), else None.
    """
    fp = _source_fingerprint(agent_name, slug)
    cache_path = SOURCES_DIR / slug / f"{agent_name.replace('-', '_')}_cache_{fp}.json"
    if not cache_path.exists():
        return None
    # Check TTL
    age = time.time() - cache_path.stat().st_mtime
    if age > SUBAGENT_CACHE_TTL:
        return None
    try:
        data = json.loads(cache_path.read_text())
        if data.get("status") in ("insufficient_data", "error", "parse_error"):
            return None  # Don't cache failures
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _write_subagent_cache(agent_name: str, slug: str, result: dict) -> None:
    """Write subagent output to a fingerprinted cache file."""
    if result.get("status") in ("insufficient_data", "error", "parse_error"):
        return  # Don't cache failures
    fp = _source_fingerprint(agent_name, slug)
    cache_path = SOURCES_DIR / slug / f"{agent_name.replace('-', '_')}_cache_{fp}.json"
    cache_path.write_text(json.dumps(result, indent=2, default=str))


def run_subagent(agent_name: str, slug: str, *, use_cache: bool = True) -> tuple[dict, bool]:
    """Run a Sonnet subagent. Returns (parsed JSON output, was_cached).

    If use_cache=True, checks for a cached output matching the current source
    fingerprint before making an API call.
    """
    # Check subagent cache first
    if use_cache:
        cached = _check_subagent_cache(agent_name, slug)
        if cached is not None:
            return cached, True

    if not anthropic or not os.environ.get("ANTHROPIC_API_KEY"):
        return {"status": "insufficient_data", "reason": "ANTHROPIC_API_KEY not set"}, False

    system_prompt = read_agent_prompt(agent_name)
    source_data = _load_source_data_for_agent(agent_name, slug)

    # Load persona for prompt caching (separate from source_data injection)
    persona_text = None
    if PERSONA_PATH.exists():
        try:
            persona_text = PERSONA_PATH.read_text()
        except OSError:
            pass

    # Remove persona from source_data user message (it's now in the cached system prompt)
    # Build user message with only source files
    user_parts = []
    source_files = SUBAGENT_SOURCE_FILES.get(agent_name, [])
    sources_dir = SOURCES_DIR / slug
    for filename in source_files:
        path = sources_dir / filename
        if path.exists():
            try:
                data = path.read_text()
                if len(data) > 50000:
                    data = data[:50000] + "\n... [truncated]"
                user_parts.append(f"=== sources/{slug}/{filename} ===\n{data}")
            except OSError:
                user_parts.append(f"=== sources/{slug}/{filename} ===\n[ERROR: could not read file]")
        else:
            user_parts.append(f"=== sources/{slug}/{filename} ===\n[FILE NOT FOUND]")

    user_msg = (
        f"Company slug: {slug}\n\n"
        f"Below is the cached source data. Analyze it and produce the structured JSON output per your instructions.\n\n"
        + "\n\n".join(user_parts)
    )

    # Use structured system with cache_control for prompt caching
    cached_system = _build_cached_system(system_prompt, persona_text)

    client = anthropic.Anthropic()
    try:
        text = _stream_anthropic_with_retry(
            client, model=SONNET_MODEL, max_tokens=8000,
            system=cached_system, messages=[{"role": "user", "content": user_msg}],
            label=agent_name,
        )
    except (anthropic.RateLimitError, anthropic.APIStatusError):
        return {"status": "insufficient_data", "reason": f"rate_limited_after_{len(RETRY_DELAYS) + 1}_retries"}, False

    # Try to parse JSON from response
    # Strip markdown fences if present
    text = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
    text = re.sub(r'\n?```\s*$', '', text.strip())

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in text
        match = re.search(r'\{.*\}', text, re.S)
        if match:
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError:
                result = None
        else:
            result = None

    if result is None:
        return {"status": "parse_error", "raw": text[:2000]}, False

    # Cache successful result
    _write_subagent_cache(agent_name, slug, result)
    return result, False


def _run_subagent_with_status(name: str, slug: str, use_cache: bool) -> tuple[str, dict, bool]:
    """Wrapper that emits status events around a subagent run. Returns (name, result, was_cached)."""
    _write_status("phase2", f"Running {name}...", subagent=name, subagent_status="running")
    try:
        result, was_cached = run_subagent(name, slug, use_cache=use_cache)
        status = result.get("status", "ok")
        if was_cached:
            print(f"    {SYM_CACHED} {name:<22} [cached subagent]", flush=True)
            _write_status("phase2", f"{name} cached", subagent=name, subagent_status="cached")
        elif status in ("insufficient_data", "parse_error"):
            print(f"    {SYM_INSUF} {name:<22} {status}: {result.get('reason', result.get('raw', '')[:60])}", flush=True)
            _write_status("phase2", f"{name} {status}", subagent=name, subagent_status="error")
        else:
            print(f"    {SYM_OK} {name:<22} ok", flush=True)
            _write_status("phase2", f"{name} complete", subagent=name, subagent_status="complete")
        return name, result, was_cached
    except Exception as e:
        print(f"    {SYM_ERROR} {name:<22} {str(e)[:60]}", flush=True)
        _write_status("phase2", f"{name} error: {str(e)[:60]}", subagent=name, subagent_status="error")
        return name, {"status": "error", "reason": str(e)[:200]}, False


def run_phase2(slug: str, *, use_cache: bool = True, parallel: bool = True) -> dict:
    """Run 3 subagents in parallel (default) or sequentially.

    Parallel execution cuts Phase 2 from ~165s to ~60s on fresh runs.
    Use parallel=False (--sequential flag) as fallback if rate limits occur.

    If use_cache=True, checks for cached subagent outputs matching the current
    source fingerprint before making API calls.
    """
    mode = "parallel" if parallel else "sequential"
    print(f"\n  Phase 2 — Subagent Synthesis (Sonnet, {mode})")
    print("  " + "─" * 50)

    agents = ["company-bg", "tech-and-pain", "hiring-signals"]
    results = {}

    if parallel:
        with ThreadPoolExecutor(max_workers=3) as pool:
            # Stagger submissions by 4s to spread across rate-limit window
            futures = {}
            for i, name in enumerate(agents):
                if i > 0:
                    time.sleep(4.0)
                futures[pool.submit(_run_subagent_with_status, name, slug, use_cache)] = name
            for future in as_completed(futures):
                name, result, was_cached = future.result()
                result["_was_cached"] = was_cached
                results[name] = result
    else:
        for i, name in enumerate(agents):
            if i > 0:
                time.sleep(2)
            name, result, was_cached = _run_subagent_with_status(name, slug, use_cache)
            result["_was_cached"] = was_cached
            results[name] = result

    # Write subagent outputs to sources/{slug}/ (strip internal _was_cached flag)
    sources_dir = SOURCES_DIR / slug
    sources_dir.mkdir(parents=True, exist_ok=True)
    for name, data in results.items():
        clean = {k: v for k, v in data.items() if k != "_was_cached"}
        out_path = sources_dir / f"{name.replace('-', '_')}.json"
        out_path.write_text(json.dumps(clean, indent=2, default=str))

    # Strip _was_cached before returning
    return {name: {k: v for k, v in data.items() if k != "_was_cached"} for name, data in results.items()}


# ---------------------------------------------------------------------------
# Phase 3 — Synthesizer (Opus)
# ---------------------------------------------------------------------------

def _parse_json_robust(text: str, raw_path: Path) -> dict | None:
    """Parse JSON from LLM output with progressive fallbacks.

    1. Strict json.loads() — fast path.
    2. Extract JSON object via regex, then strict parse.
    3. json-repair library — handles trailing commas, missing commas,
       unescaped quotes, unterminated strings, smart quotes.
    4. Return None if all fail.
    """
    # 1. Strict parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Regex extract + strict parse
    match = re.search(r'\{.*\}', text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # 3. json-repair fallback
    try:
        from json_repair import repair_json
        repaired = repair_json(text, return_objects=False)
        result = json.loads(repaired)
        # Count differences to report issue count
        diff_count = sum(1 for a, b in zip(text, repaired) if a != b)
        diff_count += abs(len(text) - len(repaired))
        print(f"    \033[33m⚠\033[0m  JSON repaired (had ~{diff_count} issues)", flush=True)
        return result
    except Exception:
        pass

    # 4. All parsers failed
    print(f"    {SYM_ERROR} synthesizer returned unparseable output", flush=True)
    print(f"    Raw output saved to {raw_path} for manual recovery.", flush=True)
    return None


def _build_company_bg_fallback(slug: str) -> dict:
    """Build a minimal company-bg output from Phase 1 cached source data.

    Used when the company-bg subagent fails (e.g., rate limited) but Phase 1
    data is available. Extracts basic info from sec.json, nces.json, sam.json,
    website.json, and news.json so Phase 3 can still run with reduced quality.
    """
    fallback = {"_fallback": True, "status": "partial_fallback"}
    sources_dir = SOURCES_DIR / slug

    # Try SEC data for basic company info
    sec_path = sources_dir / "sec.json"
    if sec_path.exists():
        try:
            sec = json.loads(sec_path.read_text())
            if sec.get("company"):
                fallback["company_name"] = sec["company"].get("name", "")
                fallback["sic_code"] = sec["company"].get("sic", "")
                fallback["state"] = sec["company"].get("state", "")
                fallback["industry"] = sec["company"].get("industry", "")
                fallback["ticker"] = sec["company"].get("ticker")
        except Exception:
            pass

    # Try NCES for K-12 info
    nces_path = sources_dir / "nces.json"
    if nces_path.exists():
        try:
            nces = json.loads(nces_path.read_text())
            if nces.get("district_metadata"):
                fallback["district_metadata"] = nces["district_metadata"]
                fallback["school_count"] = nces.get("school_count", 0)
        except Exception:
            pass

    # Try SAM for entity info
    sam_path = sources_dir / "sam.json"
    if sam_path.exists():
        try:
            sam = json.loads(sam_path.read_text())
            if sam.get("status") != "insufficient_data":
                fallback["sam_data"] = {
                    "entity_name": sam.get("entity_name", ""),
                    "naics_codes": sam.get("naics_codes", []),
                }
        except Exception:
            pass

    # Try website data — the universal floor
    website_path = sources_dir / "website.json"
    if website_path.exists():
        try:
            website = json.loads(website_path.read_text())
            if website.get("status") == "ok":
                fallback["domain"] = website.get("domain", "")
                # Extract first 2000 chars of homepage as summary
                pages = website.get("pages", {})
                for url, text in pages.items():
                    if url.endswith("/") or url.endswith(website.get("domain", "")):
                        fallback["website_summary"] = text[:2000]
                        break
                if "website_summary" not in fallback and pages:
                    fallback["website_summary"] = list(pages.values())[0][:2000]
        except Exception:
            pass

    # Try news for recent summary
    news_path = sources_dir / "news.json"
    if news_path.exists():
        try:
            news = json.loads(news_path.read_text())
            articles = news.get("articles", [])
            if articles:
                fallback["recent_headlines"] = [
                    a.get("title", "") for a in articles[:5] if a.get("title")
                ]
        except Exception:
            pass

    # Try jobs for size indicator
    jobs_path = sources_dir / "jobs.json"
    if jobs_path.exists():
        try:
            jobs = json.loads(jobs_path.read_text())
            if jobs.get("status") != "insufficient_data":
                fallback["posting_count"] = jobs.get("total_postings", 0)
        except Exception:
            pass

    fallback["_note"] = (
        "Generated from raw Phase 1 sources due to subagent failure. "
        "Re-run with --retry-phase-2 for full synthesis."
    )
    return fallback


def _build_tech_pain_fallback(slug: str) -> dict:
    """Build minimal tech-and-pain output from raw Phase 1 data."""
    fallback = {"_fallback": True, "status": "partial_fallback"}
    sources_dir = SOURCES_DIR / slug

    # Reddit for practitioner sentiment
    reddit_path = sources_dir / "reddit.json"
    if reddit_path.exists():
        try:
            data = json.loads(reddit_path.read_text())
            if data.get("status") != "insufficient_data":
                fallback["reddit_themes"] = data.get("top_pain_themes", [])[:5]
        except Exception:
            pass

    # SSL certs for tech stack hints
    ssl_path = sources_dir / "ssl.json"
    if ssl_path.exists():
        try:
            data = json.loads(ssl_path.read_text())
            if data.get("status") != "insufficient_data":
                fallback["domains_found"] = data.get("domain_count", 0)
        except Exception:
            pass

    # GitHub for tech signals
    github_path = sources_dir / "github.json"
    if github_path.exists():
        try:
            data = json.loads(github_path.read_text())
            if data.get("status") != "insufficient_data":
                fallback["github_repos"] = data.get("repo_count", 0)
        except Exception:
            pass

    fallback["_note"] = "Fallback from raw sources — re-run with --retry-phase-2 for full analysis."
    return fallback


def _build_hiring_fallback(slug: str) -> dict:
    """Build minimal hiring-signals output from raw Phase 1 data."""
    fallback = {"_fallback": True, "status": "partial_fallback"}
    sources_dir = SOURCES_DIR / slug

    jobs_path = sources_dir / "jobs.json"
    if jobs_path.exists():
        try:
            data = json.loads(jobs_path.read_text())
            if data.get("status") != "insufficient_data":
                fallback["total_postings"] = data.get("total_postings", 0)
                fallback["security_postings"] = data.get("security_related", 0)
                fallback["sample_titles"] = [
                    p.get("title", "") for p in data.get("postings", [])[:5]
                ]
        except Exception:
            pass

    fallback["_note"] = "Fallback from raw sources — re-run with --retry-phase-2 for full analysis."
    return fallback


def _build_phase3_context(slug: str, subagent_outputs: dict) -> tuple[list, str, str | None]:
    """Build the shared user message and persona text for Phase 3 calls.

    Returns (user_parts, user_msg, persona_text).
    Both Call A (main_brief) and Call B (deals_synthesizer) receive identical context.
    """
    user_parts = []
    for name in ["company-bg", "tech-and-pain", "hiring-signals"]:
        data = subagent_outputs.get(name, {"status": "insufficient_data"})
        user_parts.append(f"=== {name} OUTPUT ===\n{json.dumps(data, indent=2, default=str)}")

    # Include cooperative purchasing data if available (skip empty/failed sources)
    coop_skipped = 0
    for coop_file in ["sourcewell.json", "tips.json", "omnia.json", "hgac.json", "costars.json"]:
        coop_path = SOURCES_DIR / slug / coop_file
        if coop_path.exists():
            try:
                coop_data = json.loads(coop_path.read_text())
                if coop_data.get("status") in ("insufficient_data", "no_matches"):
                    coop_skipped += 1
                    continue
                results = coop_data.get("results_by_keyword", {})
                if not results or all(
                    r.get("status") in ("insufficient_data", "no_matches")
                    for r in results.values() if isinstance(r, dict)
                ):
                    coop_skipped += 1
                    continue
                user_parts.append(f"=== {coop_file} ===\n{json.dumps(coop_data, indent=2, default=str)}")
            except (json.JSONDecodeError, OSError):
                pass
    if coop_skipped:
        print(f"    💨 skipped {coop_skipped} empty cooperative purchasing source(s)", flush=True)

    # Include leadership data for champion identification
    leadership_path = SOURCES_DIR / slug / "leadership.json"
    if leadership_path.exists():
        try:
            leadership_data = json.loads(leadership_path.read_text())
            user_parts.append(f"=== leadership.json ===\n{json.dumps(leadership_data, indent=2, default=str)}")
        except (json.JSONDecodeError, OSError):
            pass

    # Include website data for universal company info
    website_path = SOURCES_DIR / slug / "website.json"
    if website_path.exists():
        try:
            ws_data = json.loads(website_path.read_text())
            if ws_data.get("status") == "ok":
                # Truncate page content to avoid token bloat
                ws_summary = {
                    "domain": ws_data.get("domain"),
                    "pages_fetched": ws_data.get("pages_fetched"),
                    "pages": {},
                }
                for url, text in ws_data.get("pages", {}).items():
                    ws_summary["pages"][url] = text[:3000]
                user_parts.append(f"=== website.json ===\n{json.dumps(ws_summary, indent=2, default=str)}")
        except (json.JSONDecodeError, OSError):
            pass

    # Include champion signals for enriched champion scoring
    champion_signals_path = SOURCES_DIR / slug / "champion_signals.json"
    if champion_signals_path.exists():
        try:
            cs_data = json.loads(champion_signals_path.read_text())
            user_parts.append(f"=== champion_signals.json ===\n{json.dumps(cs_data, indent=2, default=str)}")
        except (json.JSONDecodeError, OSError):
            pass

    # Persona goes into cached system prompt for prompt caching
    persona_text = None
    if PERSONA_PATH.exists():
        try:
            persona_text = PERSONA_PATH.read_text()
        except OSError:
            pass

    # Include seller profile in user message (small, company-varying)
    seller_path = PROJECT_ROOT / "persona" / "seller-profile.yml"
    if seller_path.exists():
        try:
            seller_text = seller_path.read_text()
            user_parts.append(f"=== persona/seller-profile.yml ===\n{seller_text}")
        except OSError:
            pass

    user_msg = "\n\n".join(user_parts) + f"\n\nCompany slug: {slug}"
    return user_parts, user_msg, persona_text


def _run_synthesizer_call(client, *, agent_name: str, model: str, max_tokens: int,
                          cached_system: list, user_msg: str, slug: str,
                          label: str) -> dict | None:
    """Run a single synthesizer API call. Returns parsed JSON or None on failure."""
    try:
        text = _stream_anthropic_with_retry(
            client, model=model, max_tokens=max_tokens,
            system=cached_system, messages=[{"role": "user", "content": user_msg}],
            label=label,
        )
    except (anthropic.RateLimitError, anthropic.APIStatusError):
        print(f"    {SYM_ERROR} {label} rate limited after 3 retries", flush=True)
        return None

    text = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
    text = re.sub(r'\n?```\s*$', '', text.strip())

    # Save raw output before parsing
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = BRIEFS_DIR / f"{slug}-{today}.{label}.raw.txt"
    raw_path.write_text(text)

    result = _parse_json_robust(text, raw_path)
    if result is None:
        print(f"    {SYM_ERROR} {label} returned unparseable output", flush=True)
    return result


def run_phase3(slug: str, subagent_outputs: dict) -> dict:
    """Run two synthesizer calls in parallel and merge results.

    Call A (main_brief): Sonnet — generates the structural brief (all sections
    except MEDDIC, GTM, Discovery Questions).
    Call B (deals_synthesizer): Opus — generates ONLY meddic_qualification,
    verkada_gtm_strategy, discovery_questions_by_persona.

    Both calls receive identical input context. Prompt caching ensures the
    second call gets ~90% input token savings on the shared context.
    """
    print("\n  Phase 3 — Synthesizer (parallel: main_brief + deals)")
    print("  " + "─" * 50)

    if not anthropic or not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"    {SYM_ERROR} ANTHROPIC_API_KEY not set — cannot run synthesizer", flush=True)
        return {"status": "insufficient_data", "reason": "ANTHROPIC_API_KEY not set"}

    # Check subagent health — allow partial failures
    failed_agents = []
    succeeded_agents = []
    for agent_name in ["company-bg", "tech-and-pain", "hiring-signals"]:
        agent_data = subagent_outputs.get(agent_name, {})
        if agent_data.get("status") in ("insufficient_data", "error", "parse_error"):
            failed_agents.append(agent_name)
        else:
            succeeded_agents.append(agent_name)

    if len(failed_agents) == 3:
        # ALL subagents failed — build fallbacks from raw Phase 1 data
        print(f"    \033[33m⚠\033[0m  all 3 subagents failed — building fallbacks from raw data", flush=True)
        _write_status("phase3", "All subagents failed — using raw data fallbacks")
        subagent_outputs["company-bg"] = _build_company_bg_fallback(slug)
        subagent_outputs["tech-and-pain"] = _build_tech_pain_fallback(slug)
        subagent_outputs["hiring-signals"] = _build_hiring_fallback(slug)

    if "company-bg" in failed_agents and len(succeeded_agents) >= 1:
        # company-bg failed but others succeeded — build fallback snapshot from Phase 1 cached data
        print(f"    \033[33m⚠\033[0m  company-bg failed — using Phase 1 fallback snapshot", flush=True)
        _write_status("phase3", "company-bg failed — using Phase 1 fallback")
        fallback_snapshot = _build_company_bg_fallback(slug)
        subagent_outputs["company-bg"] = fallback_snapshot

    if failed_agents:
        print(f"    \033[33m⚠\033[0m  continuing with partial data (failed: {', '.join(failed_agents)})", flush=True)
        _write_status("phase3", f"Partial subagent data — failed: {', '.join(failed_agents)}",
                      subagent_partial=True, failed_agents=failed_agents)

    # Build shared context
    _, user_msg, persona_text = _build_phase3_context(slug, subagent_outputs)

    # Read agent prompts
    main_prompt = read_agent_prompt("synthesizer")
    deals_prompt = read_agent_prompt("deals-synthesizer")

    # Build cached system blocks (persona is cached, shared across both calls)
    main_system = _build_cached_system(main_prompt, persona_text)
    deals_system = _build_cached_system(deals_prompt, persona_text)

    client = anthropic.Anthropic()

    # Run both calls in parallel with 1.5s stagger
    print(f"    {SYM_RUN} running main_brief (Sonnet) + deals_synthesizer (Opus)...", flush=True)
    _write_status("phase3", "Running synthesizers in parallel...")

    main_result = None
    deals_result = None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            main_future = pool.submit(
                _run_synthesizer_call, client,
                agent_name="synthesizer", model=SONNET_MODEL, max_tokens=12000,
                cached_system=main_system, user_msg=user_msg, slug=slug,
                label="main_brief",
            )
            time.sleep(1.5)  # Stagger to avoid rate limits
            deals_future = pool.submit(
                _run_synthesizer_call, client,
                agent_name="deals-synthesizer", model=OPUS_MODEL, max_tokens=8000,
                cached_system=deals_system, user_msg=user_msg, slug=slug,
                label="deals_synthesizer",
            )

            main_result = main_future.result()
            deals_result = deals_future.result()
    except Exception as e:
        print(f"    {SYM_ERROR} Phase 3 executor error: {str(e)[:80]}", flush=True)

    # Call A failure = total failure (main brief is the spine)
    if main_result is None:
        _write_status("phase3", "main_brief failed — cannot generate brief")
        return {"status": "parse_error", "reason": "main_brief synthesizer failed",
                "subagent_outputs": subagent_outputs}

    brief = main_result

    # Merge Call B results into the brief
    if deals_result is not None:
        deals_sections = ["meddic_qualification", "verkada_gtm_strategy", "discovery_questions_by_persona"]
        merged_count = 0
        for section in deals_sections:
            if section in deals_result and deals_result[section]:
                brief[section] = deals_result[section]
                merged_count += 1
        print(f"    {SYM_OK} deals_synthesizer merged ({merged_count}/3 sections)", flush=True)
        _write_status("phase3_deals", f"Merged {merged_count}/3 deal sections")
    else:
        # Call B failed — set fallback values, brief is still usable
        print(f"    \033[33m⚠\033[0m  deals_synthesizer failed — MEDDIC/GTM/Discovery will be missing", flush=True)
        _write_status("phase3_deals", "deals_synthesizer failed — sections missing")
        for section in ["meddic_qualification", "verkada_gtm_strategy", "discovery_questions_by_persona"]:
            if not brief.get(section) or isinstance(brief.get(section), str):
                brief[section] = {"status": "synthesis_error", "reason": "deals_synthesizer failed"}

    # Save merged raw output for debugging
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_path = BRIEFS_DIR / f"{slug}-{today}.raw.txt"
    raw_path.write_text(json.dumps(brief, indent=2, default=str))

    # Validate required top-level sections
    REQUIRED_SECTIONS = [
        "tldr", "entity_type", "snapshot", "cooperative_purchasing",
        "champion_candidates", "discovery_questions_by_persona",
        "meddic_qualification", "verkada_gtm_strategy",
    ]
    missing = [s for s in REQUIRED_SECTIONS
                if not brief.get(s) or brief.get(s) == "insufficient_data"
                or (isinstance(brief.get(s), dict) and brief[s].get("status") == "synthesis_error")]
    if missing:
        warning = f"Missing brief sections: {', '.join(missing)}"
        print(f"    \033[33m⚠\033[0m  {warning}", flush=True)
        _write_status("phase3", warning, missing_sections=missing)

    print(f"    {SYM_OK} Phase 3 complete", flush=True)
    return brief


# ---------------------------------------------------------------------------
# Phase 4 — Render
# ---------------------------------------------------------------------------

def run_phase4(brief_json_path: Path, open_browser: bool) -> Path | None:
    """Render brief JSON → HTML."""
    print("\n  Phase 4 — Render")
    print("  " + "─" * 50)

    try:
        # Inject recommended products before rendering
        sys.path.insert(0, str(PROJECT_ROOT / "render"))
        from product_recommender import inject_into_brief
        if inject_into_brief(brief_json_path):
            print(f"    {SYM_OK} injected recommended_products", flush=True)

        from render import render_brief
        sys.path.pop(0)

        html_path = render_brief(brief_json_path)
        print(f"    {SYM_OK} rendered → {html_path.relative_to(PROJECT_ROOT)}", flush=True)

        if open_browser:
            webbrowser.open(f"file://{html_path}")
            print(f"    {SYM_OK} opened in browser", flush=True)

        return html_path

    except Exception as e:
        print(f"    {SYM_ERROR} render error: {str(e)[:80]}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def research(company: str, *, force: bool = False, open_browser: bool = False, use_cache: bool = True, parallel: bool = True):
    """Full /research pipeline."""
    global _status_path
    # Reset token usage for this run
    _token_usage.update({"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0})
    slug = slugify(company)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Initialize status file
    _status_path = Path(f"/tmp/orchestrator-status-{slug}.json")
    _status_path.write_text(json.dumps({
        "slug": slug,
        "company": company,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "current_phase": "starting",
        "current_message": "Initializing pipeline...",
        "tokens_input": 0, "tokens_output": 0,
        "cache_read": 0, "cache_created": 0,
        "cost_estimate": 0,
        "events": [],
    }, indent=2))
    _write_status("starting", f"Starting /research {company}", slug=slug)

    print(f"\n  {'='*54}")
    print(f"  /research {company}")
    print(f"  slug: {slug}  date: {today}")
    print(f"  {'='*54}")

    # Check for existing complete brief
    brief_path = BRIEFS_DIR / f"{slug}-{today}.json"
    if brief_path.exists() and not force:
        try:
            existing = json.loads(brief_path.read_text())
            if existing.get("status") != "insufficient_data":
                answer = input(f"\n  Brief already exists for {slug} on {today}. Re-run? [y/N] ").strip().lower()
                if answer != "y":
                    print("  Skipped.")
                    return
        except (json.JSONDecodeError, OSError):
            pass

    t0 = time.time()

    # Phase 0 — Vertical Detection (runs BEFORE source collection)
    vertical, detection_method = phase0_detect_vertical(company, slug)
    _write_status("phase0", "Vertical detection complete",
                  detected_vertical=vertical, detection_method=detection_method,
                  vertical=vertical)

    # Phase 1 — Source data collection (now vertical-aware from the start)
    _write_status("phase1", "Collecting source data...")
    t1 = time.time()
    phase1_results = run_phase1(company, slug, force, vertical=vertical)
    print(f"  Phase 1 elapsed: {time.time() - t1:.1f}s")

    # Post-Phase 1 vertical refinement: if Phase 0 returned 'unknown',
    # retry with the data Phase 1 just collected (website, news, SEC)
    if vertical == "unknown":
        refined = detect_vertical_early(company, slug)
        if refined == "unknown":
            print(f"\n  Refining vertical via LLM (Haiku) with Phase 1 data...", flush=True)
            refined = llm_classify_vertical(company, slug)
        if refined != "unknown":
            vertical = refined
            print(f"  Refined vertical: {vertical}")
            _write_status("phase0", f"Refined vertical: {vertical} (post-Phase-1)",
                          vertical=vertical, detection_method="post_phase1_refinement")

    # Phase 1b — Deferred clients (depend on Phase 1 output)
    applicable_deferred = DEFERRED_CLIENTS
    if vertical != "unknown":
        applicable_set = SOURCES_BY_VERTICAL.get(vertical, set())
        applicable_deferred = {n for n in DEFERRED_CLIENTS if n in applicable_set}
    if applicable_deferred:
        print(f"\n  Phase 1b — Deferred Clients")
        print("  " + "─" * 50)
        for name in applicable_deferred:
            # Skip champion_signals if leadership.json was fetched within 24h
            if name == "champion_signals":
                leadership_cache = SOURCES_DIR / slug / "leadership.json"
                if leadership_cache.exists():
                    age_hours = (time.time() - leadership_cache.stat().st_mtime) / 3600
                    if age_hours < 24:
                        print(f"    {SYM_CACHED} {'champion_signals':<22} skipping — leadership.json from same day, using cache", flush=True)
                        phase1_results[name] = (SYM_CACHED, "cached (leadership <24h)")
                        continue
            cname, sym, detail = run_client(name, company, slug, force)
            phase1_results[cname] = (sym, detail)
            print(f"    {sym} {cname:<22} {detail}", flush=True)

    t1_elapsed = time.time() - t1

    # Phase 2 — Subagent synthesis
    t2 = time.time()
    subagent_outputs = run_phase2(slug, use_cache=use_cache, parallel=parallel)
    t2_elapsed = time.time() - t2
    print(f"  Phase 2 elapsed: {t2_elapsed:.1f}s")

    # Phase 3 — Synthesizer
    t3 = time.time()
    brief = run_phase3(slug, subagent_outputs)
    t3_elapsed = time.time() - t3
    print(f"  Phase 3 elapsed: {t3_elapsed:.1f}s")

    # Handle synthesizer failure
    if brief.get("status") in ("insufficient_data", "error", "parse_error"):
        _write_status("error", f"Synthesizer failed: {brief.get('reason', brief.get('status'))}",
                      error_message=brief.get("reason", brief.get("status", "unknown")))
        print(f"\n  ⚠  Synthesizer failed: {brief.get('reason', brief.get('status'))}")
        print(f"  Subagent JSONs saved to sources/{slug}/ for debugging.")

        # Save partial brief — but preserve existing good brief if present
        BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
        brief_path = BRIEFS_DIR / f"{slug}-{today}.json"
        brief["metadata"] = {
            "company_slug": slug,
            "company_name": company,
            "vertical": "unknown",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "agents_used": list(subagent_outputs.keys()),
            "models_used": {"subagents": "sonnet", "synthesizer": "opus"},
            "runtime_seconds": round(time.time() - t0, 1),
            "phase1_status": {k: v[1] for k, v in phase1_results.items()},
        }

        # Check if a complete brief already exists — don't overwrite it
        if brief_path.exists():
            try:
                existing = json.loads(brief_path.read_text())
                if existing.get("status") != "insufficient_data":
                    failed_path = BRIEFS_DIR / f"{slug}-{today}.failed.json"
                    failed_path.write_text(json.dumps(brief, indent=2, default=str))
                    print(f"  ⚠  Previous brief preserved at {brief_path.relative_to(PROJECT_ROOT)}; "
                          f"current attempt saved as {failed_path.relative_to(PROJECT_ROOT)} for debugging.")
                    return
            except (json.JSONDecodeError, OSError):
                pass

        brief_path.write_text(json.dumps(brief, indent=2, default=str))
        print(f"  Partial brief saved: {brief_path.relative_to(PROJECT_ROOT)}")

        # Still try to render
        run_phase4(brief_path, open_browser)
        return

    # Inject metadata
    if "metadata" not in brief:
        brief["metadata"] = {}
    brief["metadata"]["runtime_seconds"] = round(time.time() - t0, 1)
    brief["metadata"]["phase1_status"] = {k: v[1] for k, v in phase1_results.items()}

    # Save brief JSON
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    brief_path = BRIEFS_DIR / f"{slug}-{today}.json"
    brief_path.write_text(json.dumps(brief, indent=2, default=str))
    print(f"\n  Brief JSON: {brief_path.relative_to(PROJECT_ROOT)}")

    # Phase 4 — Render
    html_path = run_phase4(brief_path, open_browser)

    # Final summary
    total = time.time() - t0
    print(f"\n  {'='*54}")
    print(f"  /research complete in {total:.1f}s")
    print(f"    Phase 1 (sources):     {t1_elapsed:>6.1f}s")
    print(f"    Phase 2 (subagents):   {t2_elapsed:>6.1f}s")
    print(f"    Phase 3 (synthesizer): {t3_elapsed:>6.1f}s")
    if html_path:
        print(f"    HTML: {html_path.relative_to(PROJECT_ROOT)}")
    # Token usage summary
    if any(_token_usage.values()):
        print(f"\n  Token Usage:")
        print(f"    Input:          {_token_usage['input']:>8,}")
        print(f"    Output:         {_token_usage['output']:>8,}")
        if _token_usage["cache_creation"]:
            print(f"    Cache created:  {_token_usage['cache_creation']:>8,}")
        if _token_usage["cache_read"]:
            print(f"    Cache read:     {_token_usage['cache_read']:>8,}  (90% savings)")
    print(f"  {'='*54}\n")

    _write_status("complete", "Research complete",
                  brief_path=str(brief_path.relative_to(PROJECT_ROOT)),
                  html_path=str(html_path.relative_to(PROJECT_ROOT)) if html_path else None,
                  runtime=round(total, 1),
                  total_cost=round(_estimate_cost(), 4))


def retry_phase2(company: str, *, open_browser: bool = False, parallel: bool = True):
    """Re-run ONLY Phase 2 + 3 + 4, using Phase 1 cached data.

    Used when Phase 2 failed due to rate limiting but Phase 1 data is still fresh.
    """
    global _status_path
    _token_usage.update({"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0})
    slug = slugify(company)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    _status_path = Path(f"/tmp/orchestrator-status-{slug}.json")

    sources_dir = SOURCES_DIR / slug
    if not sources_dir.exists():
        print(f"  ✗ No cached Phase 1 data at sources/{slug}/ — run full /research first", file=sys.stderr)
        sys.exit(1)

    print(f"\n  {'='*54}")
    print(f"  /research {company} (retry Phase 2 only)")
    print(f"  slug: {slug}  date: {today}")
    print(f"  {'='*54}")
    print(f"  Skipping Phase 1 — using cached source data")

    t0 = time.time()

    # Phase 2 — retry with no subagent cache
    t2 = time.time()
    subagent_outputs = run_phase2(slug, use_cache=False, parallel=parallel)
    t2_elapsed = time.time() - t2
    print(f"  Phase 2 elapsed: {t2_elapsed:.1f}s")

    # Phase 3
    t3 = time.time()
    brief = run_phase3(slug, subagent_outputs)
    t3_elapsed = time.time() - t3
    print(f"  Phase 3 elapsed: {t3_elapsed:.1f}s")

    if brief.get("status") in ("insufficient_data", "error", "parse_error"):
        print(f"\n  ⚠  Synthesizer failed: {brief.get('reason', brief.get('status'))}")
        BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
        brief_path = BRIEFS_DIR / f"{slug}-{today}.json"
        brief_path.write_text(json.dumps(brief, indent=2, default=str))
        return

    # Save + render
    if "metadata" not in brief:
        brief["metadata"] = {}
    brief["metadata"]["runtime_seconds"] = round(time.time() - t0, 1)
    brief["metadata"]["retry_phase2"] = True

    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    brief_path = BRIEFS_DIR / f"{slug}-{today}.json"
    brief_path.write_text(json.dumps(brief, indent=2, default=str))
    print(f"\n  Brief JSON: {brief_path.relative_to(PROJECT_ROOT)}")

    html_path = run_phase4(brief_path, open_browser)

    total = time.time() - t0
    print(f"\n  {'='*54}")
    print(f"  /research retry complete in {total:.1f}s")
    print(f"  {'='*54}\n")


def main():
    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py <company_name> [--force] [--open] [--no-cache] [--sequential] [--retry-phase-2]", file=sys.stderr)
        print("  e.g.: python orchestrator.py 'Atlanta Public Schools'", file=sys.stderr)
        print("        python orchestrator.py 'Georgia Tech' --force --open", file=sys.stderr)
        print("        python orchestrator.py 'City of Atlanta' --no-cache --sequential", file=sys.stderr)
        print("        python orchestrator.py 'Grady Memorial Hospital' --retry-phase-2", file=sys.stderr)
        sys.exit(1)

    company = sys.argv[1]
    force = "--force" in sys.argv
    open_browser = "--open" in sys.argv
    use_cache = "--no-cache" not in sys.argv
    parallel = "--sequential" not in sys.argv

    if "--retry-phase-2" in sys.argv:
        retry_phase2(company, open_browser=open_browser, parallel=parallel)
    else:
        research(company, force=force, open_browser=open_browser, use_cache=use_cache, parallel=parallel)


if __name__ == "__main__":
    main()
