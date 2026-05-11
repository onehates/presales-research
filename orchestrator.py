#!/usr/bin/env python3
"""
Orchestrator — /research pipeline entry point.

Phase 1: Run all 18 source clients in parallel (ThreadPoolExecutor).
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
                entity_type = "k12_district"
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
                    entity_type = "public_corporation"
            except (json.JSONDecodeError, OSError):
                pass
    return {"company_name": company, "entity_type": entity_type}


CLIENT_REGISTRY = {
    # (module_name, fetch_fn_name, args_factory, cache_filename)
    "sec":                  ("clients.sec",               "fetch_sec_data",                _company_args,  "sec.json"),
    "indeed":               ("clients.indeed",            "fetch_jobs_data",               _company_args,  "jobs.json"),
    "crtsh":                ("clients.crtsh",             "fetch_crtsh_data",              _crtsh_args,    "ssl.json"),
    "github":               ("clients.github",            "fetch_github_data",             _company_args,  "github.json"),
    "news":                 ("clients.news",              "fetch_news_data",               _company_args,  "news.json"),
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


COOPERATIVE_CLIENTS = {"sourcewell", "tips", "ga_procurement", "atlanta_procurement", "omnia", "costars", "hgac"}

# Clients that depend on other clients' output and must run after Phase 1
DEFERRED_CLIENTS = {"champion_signals"}


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


def run_phase1(company: str, slug: str, force: bool) -> dict:
    """Run all clients in parallel (except deferred). Returns {client_name: (symbol, detail)}."""
    results = {}
    client_names = [n for n in CLIENT_REGISTRY if n not in DEFERRED_CLIENTS]

    # Print initial grid
    print("\n  Phase 1 — Source Data Collection")
    print("  " + "─" * 50)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(run_client, name, company, slug, force): name
            for name in client_names
        }

        try:
            for future in as_completed(futures, timeout=60):
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
                    results[name] = (SYM_ERROR, "timeout (>60s)")
                    print(f"    {SYM_ERROR} {name:<22} timeout (>60s)", flush=True)
                    _write_status("phase1", f"{name}: timeout", source=name, source_status="error")

    # Summary line
    ok_count = sum(1 for s, _ in results.values() if s in (SYM_OK, SYM_CACHED))
    insuf_count = sum(1 for s, _ in results.values() if s == SYM_INSUF)
    err_count = sum(1 for s, _ in results.values() if s == SYM_ERROR)
    print(f"\n    {ok_count} sourced  {insuf_count} insufficient  {err_count} errored")

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
    "company-bg": ["sec.json", "nces.json", "clery.json", "sam.json", "news.json"],
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


RETRY_DELAYS = [5, 15, 45]


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
        return {"status": "insufficient_data", "reason": "rate_limited_after_3_retries"}, False

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
            # Stagger submissions by 1.5s to avoid per-second rate limits
            futures = {}
            for i, name in enumerate(agents):
                if i > 0:
                    time.sleep(1.5)
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

    # Check if company-bg is present (required)
    company_bg = subagent_outputs.get("company-bg", {})
    if company_bg.get("status") in ("insufficient_data", "error", "parse_error"):
        print(f"    {SYM_ERROR} company-bg missing or failed — cannot synthesize", flush=True)
        return {
            "status": "insufficient_data",
            "reason": "company-bg subagent failed — cannot generate brief without company snapshot",
            "subagent_outputs": subagent_outputs,
        }

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
        # Import render module directly
        sys.path.insert(0, str(PROJECT_ROOT / "render"))
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

    # Phase 1 — Source data collection
    _write_status("phase1", "Collecting source data...")
    t1 = time.time()
    phase1_results = run_phase1(company, slug, force)
    print(f"  Phase 1 elapsed: {time.time() - t1:.1f}s")

    # Phase 1b — Deferred clients (depend on Phase 1 output)
    if DEFERRED_CLIENTS:
        print(f"\n  Phase 1b — Deferred Clients")
        print("  " + "─" * 50)
        for name in DEFERRED_CLIENTS:
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


def main():
    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py <company_name> [--force] [--open] [--no-cache] [--sequential]", file=sys.stderr)
        print("  e.g.: python orchestrator.py 'Atlanta Public Schools'", file=sys.stderr)
        print("        python orchestrator.py 'Georgia Tech' --force --open", file=sys.stderr)
        print("        python orchestrator.py 'City of Atlanta' --no-cache --sequential", file=sys.stderr)
        sys.exit(1)

    company = sys.argv[1]
    force = "--force" in sys.argv
    open_browser = "--open" in sys.argv
    use_cache = "--no-cache" not in sys.argv
    parallel = "--sequential" not in sys.argv

    research(company, force=force, open_browser=open_browser, use_cache=use_cache, parallel=parallel)


if __name__ == "__main__":
    main()
