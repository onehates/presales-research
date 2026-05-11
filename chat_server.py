#!/usr/bin/env python3
"""
Pre-Sales Research Platform server.

Serves:
- Homepage (/) with brief listing and research launcher
- Docs (/docs-page) with platform documentation
- Chat (/chat) — conversational interface grounded in brief data
- API (/api/...) — brief CRUD, research launcher
- Status dashboard (/status.html)

Usage:
    python3 chat_server.py                  # localhost:8000
    PORT=9000 python3 chat_server.py        # custom port
"""

import glob as globmod
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

import anthropic
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
BRIEFS_DIR = PROJECT_ROOT / "briefs"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
SOURCES_DIR = PROJECT_ROOT / "sources"
CLIENTS_DIR = PROJECT_ROOT / "clients"
STATIC_DIR = PROJECT_ROOT / "static"
PRODUCTS_PATH = PROJECT_ROOT / "persona" / "verkada-products.yml"
SONNET_MODEL = "claude-sonnet-4-6"
PORT = int(os.environ.get("PORT", 8000))
STATUS_DIR = Path("/tmp")

app = FastAPI(title="Pre-Sales Research Platform", version="1.0")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Jinja2 env for rendering templates
_jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str

class ChatRequest(BaseModel):
    slug: str
    date: str
    message: str
    history: list[ChatMessage] = []

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_TEMPLATE = """You are an SE intelligence agent helping the user prepare for a sales call with {company_name}. You have access to a sourced research brief. Answer questions using ONLY this data. Cite source attribution inline (e.g., 'per the Sourcewell signal' or 'per the news.json finding'). Be direct, technical, and brief — these are SE prep questions, not customer-facing copy. If asked about something not in the brief, say 'not in the current data' rather than making up details. Never invent quotes, names, dates, or facts not in the brief.

When asked to roleplay or simulate objections, ground your responses in the specific data from this brief — use real company details, real signal data, and real competitive intel from the brief.

When asked about champion candidates, reference the specific individuals, their scores, and score breakdowns from the brief data.

When asked about discovery questions, reference the trigger-sourced questions from the brief, including the specific trigger ID and evidence.

=== FULL BRIEF DATA ===
{brief_json}
=== END BRIEF DATA ==="""

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(req: ChatRequest):
    # Find most recent non-failed brief for this slug
    try:
        candidates = [p for p in sorted(
            BRIEFS_DIR.glob(f"{req.slug}-*.json"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        ) if ".failed." not in p.name]
        if not candidates:
            raise FileNotFoundError(f"No briefs matching {req.slug}-*.json")
        brief_path = candidates[0]
        brief_data = json.loads(brief_path.read_text())
        if not isinstance(brief_data, dict) or not brief_data.get("snapshot"):
            raise ValueError("Brief missing required 'snapshot' field")
    except Exception as e:
        error_msg = f"Brief data is unavailable or corrupted for this account. Try re-running /research, or check briefs/ for {req.slug}. ({e})"
        def error_stream():
            yield f"data: {json.dumps({'text': error_msg})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    company_name = brief_data.get("snapshot", {}).get("name", req.slug)
    brief_size = len(json.dumps(brief_data))
    key_count = len(brief_data)
    print(f"[chat] loaded brief for {req.slug}: {brief_path.name} ({brief_size} bytes, {key_count} keys)", flush=True)

    system_prompt = SYSTEM_TEMPLATE.format(
        company_name=company_name,
        brief_json=json.dumps(brief_data, indent=2, default=str),
    )

    messages = [{"role": m.role, "content": m.content} for m in req.history]
    messages.append({"role": "user", "content": req.message})

    client = anthropic.Anthropic(timeout=60.0)

    def generate():
        try:
            with client.messages.stream(
                model=SONNET_MODEL,
                max_tokens=2000,
                system=system_prompt,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
        except anthropic.APITimeoutError:
            yield f"data: {json.dumps({'text': '\n\nRequest timed out -- try a shorter question or retry.'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'text': f'\n\nChat error: {str(e)[:100]}'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Status dashboard
# ---------------------------------------------------------------------------

@app.get("/status")
async def status(slug: str = Query(default=None)):
    """Return orchestrator status JSON. If no slug, return most recent."""
    if slug:
        path = STATUS_DIR / f"orchestrator-status-{slug}.json"
    else:
        # Find most recent status file
        files = sorted(STATUS_DIR.glob("orchestrator-status-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return JSONResponse({"error": "No active runs found"}, status_code=404)
        path = files[0]
    if not path.exists():
        return JSONResponse({"error": f"No status for slug '{slug}'"}, status_code=404)
    return JSONResponse(json.loads(path.read_text()))


@app.get("/status.html")
async def status_html():
    """Serve the status dashboard HTML."""
    path = TEMPLATES_DIR / "status.html"
    if not path.exists():
        return HTMLResponse("<h1>status.html not found</h1>", status_code=404)
    return HTMLResponse(path.read_text())


# ---------------------------------------------------------------------------
# Battlecard page
# ---------------------------------------------------------------------------

@app.get("/briefs/{slug}-{date}.battlecard.html", response_class=HTMLResponse)
async def battlecard_page(slug: str, date: str):
    if ".." in slug or ".." in date:
        return HTMLResponse("<h1>Invalid</h1>", status_code=400)
    brief_path = BRIEFS_DIR / f"{slug}-{date}.json"
    if not brief_path.exists():
        return HTMLResponse("<h1>Brief not found</h1>", status_code=404)
    try:
        brief_data = json.loads(brief_path.read_text())
    except Exception:
        return HTMLResponse("<h1>Brief corrupted</h1>", status_code=500)
    template = _jinja_env.get_template("battlecard.html")
    return template.render(data=brief_data, slug=slug, date=date)


@app.get("/briefs/{slug}-{date}.salesreport.html", response_class=HTMLResponse)
async def salesreport_page(slug: str, date: str):
    if ".." in slug or ".." in date:
        return HTMLResponse("<h1>Invalid</h1>", status_code=400)
    brief_path = BRIEFS_DIR / f"{slug}-{date}.json"
    if not brief_path.exists():
        return HTMLResponse("<h1>Brief not found</h1>", status_code=404)
    try:
        brief_data = json.loads(brief_path.read_text())
    except Exception:
        return HTMLResponse("<h1>Brief corrupted</h1>", status_code=500)
    template = _jinja_env.get_template("salesreport.html")
    return template.render(data=brief_data, slug=slug, date=date)


# ---------------------------------------------------------------------------
# Brief file serving
# ---------------------------------------------------------------------------

@app.get("/briefs/{filename}")
async def serve_brief(filename: str):
    """Serve brief HTML/JSON files with path traversal protection."""
    if ".." in filename or "/" in filename or "\\" in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    if not filename.endswith((".html", ".json")):
        return JSONResponse({"error": "Only .html and .json files allowed"}, status_code=403)
    path = BRIEFS_DIR / filename
    if not path.exists():
        return JSONResponse({"error": f"Brief not found: {filename}"}, status_code=404)
    if filename.endswith(".html"):
        return FileResponse(path, media_type="text/html")
    return JSONResponse(json.loads(path.read_text()))


# ---------------------------------------------------------------------------
# Brief metadata helpers
# ---------------------------------------------------------------------------

def _load_brief_metadata(brief_path: Path) -> dict | None:
    """Extract card metadata from a brief JSON + optional .meta.json."""
    try:
        data = json.loads(brief_path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("snapshot"):
        return None

    snap = data.get("snapshot", {})
    meta = data.get("metadata", {})
    champs = data.get("champion_candidates", [])
    top_champ = champs[0] if champs else {}

    slug = meta.get("company_slug", brief_path.stem.rsplit("-", 3)[0])
    date_str = meta.get("generated_at", "")[:10]

    result = {
        "filename": brief_path.name,
        "slug": slug,
        "date": date_str,
        "name": snap.get("name", slug),
        "vertical": snap.get("vertical", ""),
        "entity_type": data.get("entity_type", ""),
        "size_indicator": snap.get("size_indicator", ""),
        "headquarters_state": snap.get("headquarters_state", ""),
        "generated_at": meta.get("generated_at", ""),
        "top_champion": top_champ.get("name", ""),
        "top_champion_score": top_champ.get("champion_fit_score", 0),
        "agents_used": meta.get("agents_used", []),
        "runtime_seconds": meta.get("runtime_seconds", 0),
        "html_exists": brief_path.with_suffix(".html").exists(),
    }

    # Load .meta.json overlay (tags, starred, archived)
    meta_path = brief_path.with_name(brief_path.stem + ".meta.json")
    if meta_path.exists():
        try:
            overlay = json.loads(meta_path.read_text())
            result["tags"] = overlay.get("tags", [])
            result["starred"] = overlay.get("starred", False)
            result["archived"] = overlay.get("archived", False)
        except Exception:
            pass
    result.setdefault("tags", [])
    result.setdefault("starred", False)
    result.setdefault("archived", False)

    return result


def _list_briefs(include_archived: bool = False) -> list[dict]:
    """List all briefs with metadata, newest first."""
    results = []
    for p in BRIEFS_DIR.glob("*.json"):
        if ".failed." in p.name or ".meta." in p.name:
            continue
        m = _load_brief_metadata(p)
        if m and (include_archived or not m.get("archived")):
            results.append(m)
    results.sort(key=lambda x: x.get("generated_at", ""), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Homepage
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def homepage():
    briefs = _list_briefs()
    verticals = sorted(set(b["vertical"] for b in briefs if b["vertical"]))
    template = _jinja_env.get_template("index.html")
    return template.render(
        briefs=briefs,
        verticals=verticals,
        total_briefs=len(briefs),
        unique_accounts=len(set(b["slug"] for b in briefs)),
        today=datetime.now().strftime("%B %d, %Y"),
    )


# ---------------------------------------------------------------------------
# Documentation page
# ---------------------------------------------------------------------------

@app.get("/docs", response_class=HTMLResponse)
@app.get("/docs-page", response_class=HTMLResponse)
async def docs_page():
    # Collect source client info
    clients = []
    if CLIENTS_DIR.exists():
        for p in sorted(CLIENTS_DIR.glob("*.py")):
            if p.name.startswith("_"):
                continue
            name = p.stem
            # Read first docstring line for description
            desc = ""
            try:
                text = p.read_text()
                if '"""' in text:
                    start = text.index('"""') + 3
                    end = text.index('"""', start)
                    desc = text[start:end].strip().split("\n")[0]
            except Exception:
                pass
            clients.append({"name": name, "description": desc, "file": p.name})

    template = _jinja_env.get_template("docs.html")
    return template.render(clients=clients)


# ---------------------------------------------------------------------------
# Product catalog page
# ---------------------------------------------------------------------------

@app.get("/products", response_class=HTMLResponse)
async def products_page():
    products = []
    categories = {}
    if PRODUCTS_PATH.exists():
        try:
            data = yaml.safe_load(PRODUCTS_PATH.read_text())
            products = data.get("products", [])
            categories = data.get("categories", {})
        except Exception:
            pass
    template = _jinja_env.get_template("products.html")
    return template.render(products=products, categories=categories)


# ---------------------------------------------------------------------------
# API: Brief list
# ---------------------------------------------------------------------------

@app.get("/api/briefs")
async def api_list_briefs(include_archived: bool = False):
    return _list_briefs(include_archived)


# ---------------------------------------------------------------------------
# API: Launch research
# ---------------------------------------------------------------------------

class ResearchRequest(BaseModel):
    company: str
    force: bool = False
    use_cache: bool = True

@app.post("/api/research")
async def api_research(req: ResearchRequest):
    slug = req.company.lower().replace(" ", "-").replace(",", "").replace(".", "")
    cmd = [sys.executable, str(PROJECT_ROOT / "orchestrator.py"), req.company]
    if req.force:
        cmd.append("--force")
    if not req.use_cache:
        cmd.append("--no-cache")

    subprocess.Popen(
        cmd,
        env=os.environ.copy(),
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"slug": slug, "status_url": f"/status.html?slug={slug}"}


# ---------------------------------------------------------------------------
# API: Delete brief
# ---------------------------------------------------------------------------

@app.delete("/api/briefs/{slug}/{date}")
async def api_delete_brief(slug: str, date: str):
    if ".." in slug or ".." in date:
        return JSONResponse({"error": "Invalid"}, status_code=400)
    stem = f"{slug}-{date}"
    deleted = []
    for suffix in [".json", ".html", ".failed.json"]:
        p = BRIEFS_DIR / (stem + suffix)
        if p.exists():
            p.unlink()
            deleted.append(p.name)
    meta = BRIEFS_DIR / (stem + ".meta.json")
    if meta.exists():
        meta.unlink()
        deleted.append(meta.name)
    if not deleted:
        return JSONResponse({"error": "Brief not found"}, status_code=404)
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# API: Update brief metadata (tags, star, archive)
# ---------------------------------------------------------------------------

class BriefMetaUpdate(BaseModel):
    tags: list[str] | None = None
    starred: bool | None = None
    archived: bool | None = None

@app.patch("/api/briefs/{slug}/{date}")
async def api_patch_brief(slug: str, date: str, update: BriefMetaUpdate):
    if ".." in slug or ".." in date:
        return JSONResponse({"error": "Invalid"}, status_code=400)
    stem = f"{slug}-{date}"
    brief_path = BRIEFS_DIR / (stem + ".json")
    if not brief_path.exists():
        return JSONResponse({"error": "Brief not found"}, status_code=404)
    meta_path = BRIEFS_DIR / (stem + ".meta.json")
    existing = {}
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text())
        except Exception:
            pass
    if update.tags is not None:
        existing["tags"] = update.tags
    if update.starred is not None:
        existing["starred"] = update.starred
    if update.archived is not None:
        existing["archived"] = update.archived
    meta_path.write_text(json.dumps(existing, indent=2))
    return existing


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "briefs": len(list(BRIEFS_DIR.glob("*.json")))}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    print(f"Chat server starting on http://localhost:{PORT}")
    print(f"Briefs directory: {BRIEFS_DIR}")
    print(f"Available briefs: {[p.name for p in BRIEFS_DIR.glob('*.json')]}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
