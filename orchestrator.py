#!/usr/bin/env python3
"""
Orchestrator — /research pipeline entry point.

Phase 1: Run all 14 source clients in parallel (ThreadPoolExecutor).
Phase 2: Run 3 subagents (company-bg, tech-and-pain, hiring-signals) in parallel.
Phase 3: Run synthesizer (Opus) reading all 3 subagent outputs + persona.
Phase 4: Render brief JSON → HTML via render/render.py.

Usage:
    python orchestrator.py "Atlanta Public Schools" [--force] [--open]
"""

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

SONNET_MODEL = "claude-sonnet-4-20250514"
OPUS_MODEL = "claude-opus-4-20250514"

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


def run_client(client_name: str, company: str, slug: str, force: bool) -> tuple[str, str, str]:
    """
    Run a single client. Returns (client_name, status_symbol, detail).
    """
    mod_name, fn_name, args_factory, cache_file = CLIENT_REGISTRY[client_name]

    try:
        # Check if already cached (for reporting)
        cache_path = SOURCES_DIR / slug / cache_file
        was_cached = cache_path.exists() and not force

        # Import and call
        import importlib
        mod = importlib.import_module(mod_name)
        fetch_fn = getattr(mod, fn_name)

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


def run_phase1(company: str, slug: str, force: bool) -> dict:
    """Run all 14 clients in parallel. Returns {client_name: (symbol, detail)}."""
    results = {}
    client_names = list(CLIENT_REGISTRY.keys())

    # Print initial grid
    print("\n  Phase 1 — Source Data Collection")
    print("  " + "─" * 50)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(run_client, name, company, slug, force): name
            for name in client_names
        }

        for future in as_completed(futures):
            name, symbol, detail = future.result()
            results[name] = (symbol, detail)
            # Live update
            print(f"    {symbol} {name:<22} {detail}", flush=True)

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


def run_subagent(agent_name: str, slug: str) -> dict:
    """Run a Sonnet subagent. Returns the parsed JSON output."""
    if not anthropic or not os.environ.get("ANTHROPIC_API_KEY"):
        return {"status": "insufficient_data", "reason": "ANTHROPIC_API_KEY not set"}

    system_prompt = read_agent_prompt(agent_name)
    user_msg = f"Company slug: {slug}\n\nAnalyze the cached source data for this company and produce the structured JSON output per your instructions."

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=8000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = msg.content[0].text

    # Try to parse JSON from response
    # Strip markdown fences if present
    text = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
    text = re.sub(r'\n?```\s*$', '', text.strip())

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in text
        match = re.search(r'\{.*\}', text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {"status": "parse_error", "raw": text[:2000]}


def run_phase2(slug: str) -> dict:
    """Run 3 subagents in parallel. Returns {agent_name: parsed_json}."""
    print("\n  Phase 2 — Subagent Synthesis (Sonnet)")
    print("  " + "─" * 50)

    agents = ["company-bg", "tech-and-pain", "hiring-signals"]
    results = {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(run_subagent, name, slug): name
            for name in agents
        }

        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                status = result.get("status", "ok")
                if status in ("insufficient_data", "parse_error"):
                    print(f"    {SYM_INSUF} {name:<22} {status}: {result.get('reason', result.get('raw', '')[:60])}", flush=True)
                else:
                    print(f"    {SYM_OK} {name:<22} ok", flush=True)
                results[name] = result
            except Exception as e:
                print(f"    {SYM_ERROR} {name:<22} {str(e)[:60]}", flush=True)
                results[name] = {"status": "error", "reason": str(e)[:200]}

    # Write subagent outputs to sources/{slug}/
    sources_dir = SOURCES_DIR / slug
    sources_dir.mkdir(parents=True, exist_ok=True)
    for name, data in results.items():
        out_path = sources_dir / f"{name.replace('-', '_')}.json"
        out_path.write_text(json.dumps(data, indent=2, default=str))

    return results


# ---------------------------------------------------------------------------
# Phase 3 — Synthesizer (Opus)
# ---------------------------------------------------------------------------

def run_phase3(slug: str, subagent_outputs: dict) -> dict:
    """Run the synthesizer with all 3 subagent outputs."""
    print("\n  Phase 3 — Synthesizer (Opus)")
    print("  " + "─" * 50)

    if not anthropic or not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"    {SYM_ERROR} ANTHROPIC_API_KEY not set — cannot run synthesizer", flush=True)
        return {"status": "insufficient_data", "reason": "ANTHROPIC_API_KEY not set"}

    # Check if company-bg is present (required)
    company_bg = subagent_outputs.get("company-bg", {})
    if company_bg.get("status") in ("insufficient_data", "error", "parse_error"):
        print(f"    {SYM_ERROR} company-bg missing or failed — cannot synthesize", flush=True)
        # Return partial data for debugging
        return {
            "status": "insufficient_data",
            "reason": "company-bg subagent failed — cannot generate brief without company snapshot",
            "subagent_outputs": subagent_outputs,
        }

    system_prompt = read_agent_prompt("synthesizer")

    # Build user message with all 3 subagent outputs
    user_parts = []
    for name in ["company-bg", "tech-and-pain", "hiring-signals"]:
        data = subagent_outputs.get(name, {"status": "insufficient_data"})
        user_parts.append(f"=== {name} OUTPUT ===\n{json.dumps(data, indent=2, default=str)}")

    user_msg = "\n\n".join(user_parts) + f"\n\nCompany slug: {slug}"

    print(f"    {SYM_RUN} running synthesizer...", flush=True)

    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=12000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )

        text = msg.content[0].text
        text = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
        text = re.sub(r'\n?```\s*$', '', text.strip())

        try:
            brief = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', text, re.S)
            if match:
                brief = json.loads(match.group(0))
            else:
                print(f"    {SYM_ERROR} synthesizer returned unparseable output", flush=True)
                return {"status": "parse_error", "raw": text[:2000], "subagent_outputs": subagent_outputs}

        print(f"    {SYM_OK} synthesizer complete", flush=True)
        return brief

    except Exception as e:
        print(f"    {SYM_ERROR} synthesizer error: {str(e)[:80]}", flush=True)
        return {"status": "error", "reason": str(e)[:200], "subagent_outputs": subagent_outputs}


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

def research(company: str, *, force: bool = False, open_browser: bool = False):
    """Full /research pipeline."""
    slug = slugify(company)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"\n  {'='*54}")
    print(f"  /research {company}")
    print(f"  slug: {slug}  date: {today}")
    print(f"  {'='*54}")

    t0 = time.time()

    # Phase 1 — Source data collection
    t1 = time.time()
    phase1_results = run_phase1(company, slug, force)
    t1_elapsed = time.time() - t1
    print(f"  Phase 1 elapsed: {t1_elapsed:.1f}s")

    # Phase 2 — Subagent synthesis
    t2 = time.time()
    subagent_outputs = run_phase2(slug)
    t2_elapsed = time.time() - t2
    print(f"  Phase 2 elapsed: {t2_elapsed:.1f}s")

    # Phase 3 — Synthesizer
    t3 = time.time()
    brief = run_phase3(slug, subagent_outputs)
    t3_elapsed = time.time() - t3
    print(f"  Phase 3 elapsed: {t3_elapsed:.1f}s")

    # Handle synthesizer failure
    if brief.get("status") in ("insufficient_data", "error", "parse_error"):
        print(f"\n  ⚠  Synthesizer failed: {brief.get('reason', brief.get('status'))}")
        print(f"  Subagent JSONs saved to sources/{slug}/ for debugging.")

        # Save partial brief anyway
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
    print(f"  {'='*54}\n")


def main():
    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py <company_name> [--force] [--open]", file=sys.stderr)
        print("  e.g.: python orchestrator.py 'Atlanta Public Schools'", file=sys.stderr)
        print("        python orchestrator.py 'Georgia Tech' --force --open", file=sys.stderr)
        sys.exit(1)

    company = sys.argv[1]
    force = "--force" in sys.argv
    open_browser = "--open" in sys.argv

    research(company, force=force, open_browser=open_browser)


if __name__ == "__main__":
    main()
