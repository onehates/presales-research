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

import httpx
import yaml

import anthropic
from fastapi import FastAPI, HTTPException, Query, Request
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
PERSONA_PATH = PROJECT_ROOT / "persona" / "verkada-se.yml"
SECTIONS_PATH = PROJECT_ROOT / "data" / "sales_report_sections.yml"
SONNET_MODEL = "claude-sonnet-4-6"
PORT = int(os.environ.get("PORT", 8000))
STATUS_DIR = Path("/tmp")
APP_VERSION = "1.3.0"

app = FastAPI(title="Pre-Sales Research Platform", version="1.0", docs_url="/api/docs", redoc_url=None)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Jinja2 env for rendering templates
_jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)

# Import humanize_id filter and sanitizer from render module
from render.render import humanize_id, _sanitize_data, _resolve_company_domain
_jinja_env.filters["humanize_id"] = humanize_id
_jinja_env.globals["APP_VERSION"] = APP_VERSION

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Brief loading helpers
# ---------------------------------------------------------------------------

def find_valid_brief(slug: str, date: str = None) -> tuple[Path | None, dict | None]:
    """Find the most recent valid brief for a slug.

    Skips .failed.json, .meta.json, .battlecard., .salesreport., .coach. files.
    Skips briefs with status=error/insufficient_data/parse_error.
    Requires 'snapshot' field.
    """
    if date:
        pattern = f"{slug}-{date}.json"
    else:
        pattern = f"{slug}-*.json"

    for path in sorted(BRIEFS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
        name = path.name
        if any(x in name for x in ('.failed.', '.meta.', '.battlecard.', '.salesreport.', '.discovery.', '.coach.', '.product-selection.')):
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get('status') in ('error', 'insufficient_data', 'parse_error'):
            continue
        if 'snapshot' not in data:
            continue
        return path, data

    return None, None


def archive_failed_briefs() -> int:
    """Move .failed.json and broken briefs to briefs/archive/."""
    archive = BRIEFS_DIR / "archive"
    archive.mkdir(exist_ok=True)
    moved = 0
    for path in list(BRIEFS_DIR.glob("*.json")):
        if '.failed.' in path.name:
            path.rename(archive / path.name)
            moved += 1
            continue
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data.get('status') in ('error', 'insufficient_data', 'parse_error'):
                path.rename(archive / path.name)
                moved += 1
        except Exception:
            pass
    return moved


# ---------------------------------------------------------------------------
# Vertical detection helper
# ---------------------------------------------------------------------------

_VERTICAL_SLUG_MAP = {
    "K-12": "k12", "K-12 District": "k12", "K12": "k12",
    "Higher Ed": "higher_ed", "Higher Education": "higher_ed",
    "Healthcare": "healthcare", "Hospital": "healthcare",
    "Senior Living": "senior_living",
    "State & Local Gov": "state_local_gov", "Government": "state_local_gov",
    "Federal": "federal",
    "Public Safety": "public_safety", "Law Enforcement": "public_safety",
    "Transportation": "transportation",
    "Manufacturing": "manufacturing",
    "Retail": "retail",
    "Hospitality": "hospitality",
    "Critical Infrastructure": "critical_infrastructure",
}

def _detect_vertical_slug(brief_data: dict) -> str:
    """Extract a normalized vertical slug from brief data."""
    raw = (brief_data.get("snapshot", {}).get("vertical", "")
           or brief_data.get("entity_type", "")
           or brief_data.get("vertical_match", {}).get("entity_type", ""))
    if raw in _VERTICAL_SLUG_MAP:
        return _VERTICAL_SLUG_MAP[raw]
    # Try case-insensitive
    for k, v in _VERTICAL_SLUG_MAP.items():
        if k.lower() == raw.lower():
            return v
    # Already a slug?
    if raw.lower().replace(" ", "_") in [v for v in _VERTICAL_SLUG_MAP.values()]:
        return raw.lower().replace(" ", "_")
    return raw.lower().replace(" ", "_") if raw else "unknown"


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
    brief_path, brief_data = find_valid_brief(req.slug)
    if not brief_data:
        error_msg = f"No valid brief found for {req.slug}. Files in briefs/ may be failed or partial. Re-run /research to regenerate."
        def error_stream():
            yield f"data: {json.dumps({'text': error_msg, 'error': True})}\n\n"
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

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        def missing_key_stream():
            yield f"data: {json.dumps({'text': 'ANTHROPIC_API_KEY is not set. Export it in your shell and restart chat_server.py.'})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(missing_key_stream(), media_type="text/event-stream")

    client = anthropic.Anthropic(timeout=90.0)

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
        except anthropic.AuthenticationError:
            yield f"data: {json.dumps({'text': '\n\nAPI key is invalid or expired. Check your ANTHROPIC_API_KEY.', 'error': True})}\n\n"
        except anthropic.RateLimitError:
            yield f"data: {json.dumps({'text': '\n\nRate limited by Anthropic API. Wait a moment and retry.', 'error': True})}\n\n"
        except anthropic.APIConnectionError:
            yield f"data: {json.dumps({'text': '\n\nCannot reach the Anthropic API. Check your internet connection.', 'error': True})}\n\n"
        except anthropic.APITimeoutError:
            yield f"data: {json.dumps({'text': '\n\nRequest timed out — try a shorter question or retry.', 'error': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'text': f'\n\nChat error: {str(e)[:150]}', 'error': True})}\n\n"
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
# Slug-only brief resolver (handles stale localStorage entries with no date)
# ---------------------------------------------------------------------------

@app.get("/brief/{slug}")
async def brief_by_slug(slug: str):
    if ".." in slug or "/" in slug:
        return JSONResponse({"error": "Invalid slug"}, status_code=400)
    files = sorted(BRIEFS_DIR.glob(f"{slug}-*.html"), reverse=True)
    files = [f for f in files if not any(
        x in f.name for x in ('.battlecard.', '.salesreport.', '.discovery.', '.failed.', '.meta.', '.coach.')
    )]
    if not files:
        return HTMLResponse(
            f"<h1>Brief not found</h1><p>No brief exists for slug: {slug}</p>"
            f"<p><a href='/'>← Back to dashboard</a></p>",
            status_code=404,
        )
    return FileResponse(files[0], media_type="text/html")


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
        brief_data = _sanitize_data(json.loads(brief_path.read_text()))
    except Exception:
        return HTMLResponse("<h1>Brief corrupted</h1>", status_code=500)
    template = _jinja_env.get_template("battlecard.html")
    real_slug = brief_data.get("metadata", {}).get("company_slug", slug)
    real_date = (brief_data.get("metadata", {}).get("generated_at", "") or "")[:10] or date
    brief_data = _filter_products(brief_data, real_slug, real_date)
    company_domain = _resolve_company_domain(real_slug, brief_data, brief_path)
    return template.render(data=brief_data, slug=slug, date=date, company_domain=company_domain)


@app.get("/briefs/{slug}-{date}.discovery.html", response_class=HTMLResponse)
async def discovery_page(slug: str, date: str):
    if ".." in slug or ".." in date:
        return HTMLResponse("<h1>Invalid</h1>", status_code=400)
    brief_path = BRIEFS_DIR / f"{slug}-{date}.json"
    if not brief_path.exists():
        return HTMLResponse("<h1>Brief not found</h1>", status_code=404)
    try:
        brief_data = _sanitize_data(json.loads(brief_path.read_text()))
    except Exception:
        return HTMLResponse("<h1>Brief corrupted</h1>", status_code=500)
    # Build subtitle
    snap = brief_data.get("snapshot", {})
    parts = []
    v = snap.get("vertical", "")
    if v:
        parts.append(v)
    hq_city = snap.get("headquarters_city", "")
    hq_state = snap.get("headquarters_state", "")
    if hq_city and hq_state:
        parts.append(f"{hq_city}, {hq_state}")
    elif hq_state:
        parts.append(hq_state)
    ticker = snap.get("ticker")
    if ticker and ticker != "None" and ticker.lower() != "null":
        exchange = snap.get("ticker_exchange") or "NYSE"
        parts.append(f"{exchange}: {ticker}")
    brief_subtitle = " \u00b7 ".join(parts)
    template = _jinja_env.get_template("discovery.html")
    real_slug = brief_data.get("metadata", {}).get("company_slug", slug)
    real_date = (brief_data.get("metadata", {}).get("generated_at", "") or "")[:10] or date
    brief_data = _filter_products(brief_data, real_slug, real_date)
    company_domain = _resolve_company_domain(real_slug, brief_data, brief_path)
    return template.render(data=brief_data, slug=slug, date=date, brief_subtitle=brief_subtitle, company_domain=company_domain)


@app.get("/briefs/{slug}-{date}.salesreport.html", response_class=HTMLResponse)
async def salesreport_page(slug: str, date: str):
    if ".." in slug or ".." in date:
        return HTMLResponse("<h1>Invalid</h1>", status_code=400)
    brief_path = BRIEFS_DIR / f"{slug}-{date}.json"
    if not brief_path.exists():
        return HTMLResponse("<h1>Brief not found</h1>", status_code=404)
    try:
        brief_data = _sanitize_data(json.loads(brief_path.read_text()))
    except Exception:
        return HTMLResponse("<h1>Brief corrupted</h1>", status_code=500)

    # Load vertical-aware config
    section_visibility = {}
    if SECTIONS_PATH.exists():
        try:
            section_visibility = yaml.safe_load(SECTIONS_PATH.read_text()).get("sections", {})
        except Exception:
            pass
    vertical_value_props = {}
    if PERSONA_PATH.exists():
        try:
            persona = yaml.safe_load(PERSONA_PATH.read_text())
            vertical_value_props = persona.get("vertical_value_props", {})
        except Exception:
            pass

    # Determine vertical slug
    vertical = _detect_vertical_slug(brief_data)

    template = _jinja_env.get_template("salesreport.html")
    real_slug = brief_data.get("metadata", {}).get("company_slug", slug)
    real_date = (brief_data.get("metadata", {}).get("generated_at", "") or "")[:10] or date
    brief_data = _filter_products(brief_data, real_slug, real_date)
    company_domain = _resolve_company_domain(real_slug, brief_data, brief_path)
    return template.render(
        data=brief_data, slug=slug, date=date,
        vertical=vertical,
        section_visibility=section_visibility,
        vertical_value_props=vertical_value_props,
        company_domain=company_domain,
    )


# ---------------------------------------------------------------------------
# Product selection persistence
# ---------------------------------------------------------------------------

def _load_product_deselected(slug: str, date: str) -> set:
    """Load set of deselected product IDs for a brief."""
    p = BRIEFS_DIR / f"{slug}-{date}.product-selection.json"
    if p.exists():
        try:
            return set(json.loads(p.read_text()).get("deselected", []))
        except Exception:
            pass
    return set()


def _filter_products(brief_data: dict, slug: str, date: str) -> dict:
    """Return brief_data with deselected products removed."""
    deselected = _load_product_deselected(slug, date)
    if not deselected:
        return brief_data
    rp = brief_data.get("recommended_products") or {}
    if not rp:
        return brief_data
    out = dict(brief_data)
    out["recommended_products"] = {
        "primary_bundle": [p for p in rp.get("primary_bundle", []) if p.get("product_id") not in deselected],
        "secondary_bundle": [p for p in rp.get("secondary_bundle", []) if p.get("product_id") not in deselected],
        "vertical_fit_notes": rp.get("vertical_fit_notes"),
    }
    return out


@app.post("/api/briefs/{slug}/{date}/product-selection")
async def save_product_selection(slug: str, date: str, request: Request):
    if ".." in slug or ".." in date:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    body = await request.json()
    deselected = body.get("deselected", [])
    out_path = BRIEFS_DIR / f"{slug}-{date}.product-selection.json"
    out_path.write_text(json.dumps({"deselected": deselected}))
    return {"ok": True, "deselected_count": len(deselected)}


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


@app.get("/api/brief-pdf/{slug}/{date}")
async def brief_pdf(slug: str, date: str):
    """Server-side PDF generation via Playwright — no browser headers/footers."""
    if ".." in slug or ".." in date:
        raise HTTPException(400, "Invalid parameters")
    html_path = (BRIEFS_DIR / f"{slug}-{date}.html").resolve()
    if not html_path.exists():
        raise HTTPException(404, "Brief HTML not found — render it first")
    pdf_path = BRIEFS_DIR / f"{slug}-{date}.pdf"

    # Load product deselections and inject into HTML for headless rendering
    json_path = BRIEFS_DIR / f"{slug}-{date}.json"
    real_slug = slug
    real_date = date
    if json_path.exists():
        try:
            bd = json.loads(json_path.read_text())
            real_slug = bd.get("metadata", {}).get("company_slug", slug)
            real_date = (bd.get("metadata", {}).get("generated_at", "") or "")[:10] or date
        except Exception:
            pass
    deselected = _load_product_deselected(real_slug, real_date)

    import tempfile
    html_content = html_path.read_text()
    if deselected:
        # Inject a script that hides deselected products before render
        deselected_js = json.dumps(list(deselected))
        inject = (
            f'<script>document.addEventListener("DOMContentLoaded",function(){{'
            f'var ds={deselected_js};'
            f'document.querySelectorAll("[data-product-id]").forEach(function(el){{'
            f'if(ds.indexOf(el.dataset.productId)!==-1)el.style.display="none";'
            f'}});}});</script></head>'
        )
        html_content = html_content.replace('</head>', inject, 1)

    tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, dir=str(BRIEFS_DIR))
    tmp.write(html_content.encode())
    tmp.close()
    tmp_path = Path(tmp.name).resolve()

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(f"file://{tmp_path}", wait_until="networkidle", timeout=30000)
            try:
                await page.wait_for_function(
                    "Array.from(document.images).every(img => img.complete)",
                    timeout=10000
                )
            except Exception:
                pass  # proceed even if some images time out
            await page.emulate_media(media="print")
            await page.pdf(
                path=str(pdf_path),
                format="Letter",
                print_background=True,
                display_header_footer=False,
                margin={"top": "0.5in", "right": "0.5in",
                        "bottom": "0.5in", "left": "0.5in"},
            )
            await browser.close()
    finally:
        tmp_path.unlink(missing_ok=True)

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"{slug}-{date}.pdf",
    )


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
        if any(x in p.name for x in ('.failed.', '.meta.', '.battlecard.', '.salesreport.', '.discovery.', '.coach.', '.product-selection.')):
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
# Similar Accounts suggester
# ---------------------------------------------------------------------------

def _extract_slug_from_stem(stem: str) -> str:
    """Extract company slug from brief filename stem like 'target-corporation-2026-05-09'."""
    # Find the date portion (YYYY-MM-DD at end) and strip it
    import re
    m = re.match(r'^(.+?)-(\d{4}-\d{2}-\d{2})$', stem)
    return m.group(1) if m else stem


def _compute_size_tier(brief: dict) -> str:
    """Compute size tier from brief data: small / mid / large / enterprise."""
    snap = brief.get("snapshot", {})
    si = snap.get("size_indicator", "").lower()
    # Enterprise signals
    if any(x in si for x in ["large accelerated filer", "fortune ", "100,000", "200,000", "300,000", "400,000", "500,000",
                              "1,000", "2,000", "4,000"]) and any(x in si for x in ["store", "location", "employee"]):
        return "enterprise"
    # Large signals
    if any(x in si for x in ["50,", "40,", "30,", "20,", "10,000", "large", "1,000-bed", "level 1", "500+"]):
        return "large"
    # Mid
    if any(x in si for x in ["5,", "1,", "2,", "3,", "mid", "medium"]):
        return "mid"
    return "small"


_VERTICAL_PEERS: dict[str, list[dict]] = {
    "Retail": [
        {"name": "Lowe's", "reasons": ["Home improvement retail", "Large store footprint", "LP + ORC focus"]},
        {"name": "Costco", "reasons": ["Warehouse retail", "High-value inventory", "Membership-based LP"]},
        {"name": "Kroger", "reasons": ["Grocery/retail chain", "2,700+ locations", "Pharmacy + fuel security"]},
        {"name": "Best Buy", "reasons": ["Electronics retail", "High-shrink category", "ORC target"]},
        {"name": "Floor & Decor", "reasons": ["Specialty retail", "Large format stores", "Growing footprint"]},
        {"name": "Dollar General", "reasons": ["High store count (19,000+)", "Rural locations", "Safety concerns"]},
        {"name": "Walgreens", "reasons": ["Pharmacy retail", "Urban shrink hotspots", "ORC target"]},
        {"name": "TJX Companies", "reasons": ["Off-price retail", "High-volume stores", "LP challenges"]},
        {"name": "Ross Stores", "reasons": ["Off-price retail", "Multi-region footprint", "Shrink management"]},
        {"name": "Albertsons", "reasons": ["Grocery chain", "Pharmacy security", "Multi-banner operation"]},
    ],
    "K-12": [
        {"name": "Gwinnett County Public Schools", "reasons": ["Large GA district", "140+ schools", "NDAA pressure"]},
        {"name": "Fulton County Schools", "reasons": ["Large GA district", "100+ sites", "Federal funding exposure"]},
        {"name": "Cobb County School District", "reasons": ["Large GA district", "110+ schools", "Bond-funded upgrades"]},
        {"name": "DeKalb County School District", "reasons": ["Large GA district", "130+ schools", "Urban/suburban mix"]},
        {"name": "Houston ISD", "reasons": ["Mega-district", "270+ schools", "TX state funding"]},
        {"name": "Dallas ISD", "reasons": ["Large TX district", "230+ schools", "Urban security focus"]},
        {"name": "Broward County Public Schools", "reasons": ["FL mega-district", "Alyssa's Law", "MSD shooting response"]},
        {"name": "Clark County School District", "reasons": ["Largest NV district", "360+ schools", "Rapid growth"]},
        {"name": "Charlotte-Mecklenburg Schools", "reasons": ["Large NC district", "180+ schools", "Security modernization"]},
        {"name": "Fairfax County Public Schools", "reasons": ["Large VA district", "200+ schools", "NDAA compliance"]},
    ],
    "Healthcare": [
        {"name": "Piedmont Healthcare", "reasons": ["GA health system", "Multi-campus", "ED security needs"]},
        {"name": "Emory Healthcare", "reasons": ["Academic medical center", "Large GA campus", "Research facility security"]},
        {"name": "WellStar Health System", "reasons": ["Large GA system", "11 hospitals", "Campus consolidation"]},
        {"name": "Northside Hospital", "reasons": ["GA health system", "5 hospitals", "Growing footprint"]},
        {"name": "Memorial Hermann", "reasons": ["TX mega-system", "17 hospitals", "Infant security"]},
        {"name": "HCA Healthcare", "reasons": ["Largest US hospital operator", "180+ hospitals", "Standardization play"]},
        {"name": "Tenet Healthcare", "reasons": ["Large system", "60+ hospitals", "Multi-state operations"]},
        {"name": "Atrium Health", "reasons": ["Large SE system", "40+ hospitals", "Wake Forest affiliation"]},
        {"name": "Intermountain Health", "reasons": ["Large Western system", "33 hospitals", "Innovation leader"]},
        {"name": "Providence Health", "reasons": ["Large Western system", "50+ hospitals", "Multi-state"]},
    ],
    "HigherEd": [
        {"name": "Georgia State University", "reasons": ["Large GA university", "Urban campus", "Clery Act"]},
        {"name": "University of Georgia", "reasons": ["Large GA campus", "800+ acres", "Multi-building security"]},
        {"name": "Kennesaw State University", "reasons": ["Growing GA university", "Dual campus", "Rapid expansion"]},
        {"name": "Georgia Southern University", "reasons": ["Multi-campus GA university", "Growing enrollment"]},
        {"name": "Arizona State University", "reasons": ["Largest US university", "Multiple campuses", "Innovation focus"]},
        {"name": "University of Central Florida", "reasons": ["70,000+ students", "Large campus", "Urban setting"]},
        {"name": "Ohio State University", "reasons": ["Mega-campus", "60,000+ students", "Big Ten security"]},
        {"name": "Penn State University", "reasons": ["Multi-campus system", "24 campuses", "Standardization need"]},
        {"name": "University of Texas at Austin", "reasons": ["50,000+ students", "Large campus", "Urban setting"]},
        {"name": "Texas A&M University", "reasons": ["70,000+ students", "Multiple campuses", "Research security"]},
    ],
    "Manufacturing": [
        {"name": "Gulfstream Aerospace", "reasons": ["GA manufacturer", "High-security facilities", "ITAR compliance"]},
        {"name": "Lockheed Martin Marietta", "reasons": ["GA defense manufacturer", "Classified facility security"]},
        {"name": "Kia Georgia", "reasons": ["Auto manufacturing", "Large plant", "Worker safety focus"]},
        {"name": "Caterpillar", "reasons": ["Heavy equipment mfg", "Multi-site operations", "Safety compliance"]},
        {"name": "Tyson Foods", "reasons": ["Food manufacturing", "OSHA compliance", "Multi-plant operations"]},
        {"name": "Tesla Gigafactory", "reasons": ["Advanced manufacturing", "Large campus", "IT/OT convergence"]},
        {"name": "Procter & Gamble", "reasons": ["CPG manufacturer", "100+ global plants", "Environmental monitoring"]},
        {"name": "3M Company", "reasons": ["Diversified manufacturer", "Multi-site", "Environmental sensors"]},
    ],
    "CRE_Hospitality": [
        {"name": "Marriott International", "reasons": ["Largest hotel chain", "8,000+ properties", "Guest safety"]},
        {"name": "Hilton Hotels", "reasons": ["Major hotel chain", "Multi-brand portfolio", "Global operations"]},
        {"name": "CBRE Group", "reasons": ["Largest CRE firm", "Property management", "Multi-tenant access"]},
        {"name": "Prologis", "reasons": ["Industrial REIT", "Warehouse/logistics", "Perimeter security"]},
        {"name": "Simon Property Group", "reasons": ["Largest mall REIT", "Retail properties", "Common area security"]},
        {"name": "Brookfield Asset Management", "reasons": ["Major CRE owner", "Office + retail", "Multi-tenant"]},
    ],
    "Government": [
        {"name": "City of Atlanta", "reasons": ["Large GA municipality", "Multiple facilities", "Public safety"]},
        {"name": "Fulton County Government", "reasons": ["GA county government", "Courthouse security", "Multiple buildings"]},
        {"name": "City of Dallas", "reasons": ["Large TX city", "Municipal buildings", "Public safety integration"]},
        {"name": "City of Phoenix", "reasons": ["Large AZ city", "Rapid growth", "Infrastructure modernization"]},
        {"name": "City of Charlotte", "reasons": ["Large NC city", "Growing metro", "Smart city initiatives"]},
        {"name": "Miami-Dade County", "reasons": ["Large FL county", "Multi-facility", "Hurricane resilience"]},
    ],
    "SeniorLiving": [
        {"name": "Brookdale Senior Living", "reasons": ["Largest US operator", "680+ communities", "Elopement risk"]},
        {"name": "Five Star Senior Living", "reasons": ["Large operator", "Multi-state", "Access control needs"]},
        {"name": "Sunrise Senior Living", "reasons": ["Premium operator", "300+ communities", "Family trust"]},
        {"name": "Atria Senior Living", "reasons": ["200+ communities", "Growing footprint", "Standardization"]},
    ],
}

# SIC-code based peers — maps SIC to additional company suggestions
_SIC_PEERS: dict[str, list[dict]] = {
    "5211": [  # Lumber & Home Improvement
        {"name": "Lowe's", "reasons": ["Direct competitor", "Same SIC 5211", "Home improvement"]},
        {"name": "Menards", "reasons": ["Regional competitor", "Same SIC 5211", "Midwest focus"]},
        {"name": "Floor & Decor", "reasons": ["Specialty home improvement", "Large format", "Growing chain"]},
        {"name": "Tractor Supply Co.", "reasons": ["Rural home/farm retail", "2,000+ stores", "LP needs"]},
    ],
    "5331": [  # Variety Stores / General Merchandise
        {"name": "Dollar General", "reasons": ["Same SIC 5331", "19,000+ stores", "High-count footprint"]},
        {"name": "Dollar Tree", "reasons": ["Same SIC 5331", "15,000+ stores", "Shrink management"]},
        {"name": "Five Below", "reasons": ["Same SIC 5331", "Growing chain", "Value retail"]},
    ],
    "8211": [  # Elementary & Secondary Schools
        {"name": "Los Angeles Unified School District", "reasons": ["Largest CA district", "1,000+ schools", "Bond funding"]},
        {"name": "Chicago Public Schools", "reasons": ["3rd largest US district", "600+ schools", "Safety focus"]},
        {"name": "Miami-Dade County Public Schools", "reasons": ["4th largest US district", "400+ schools", "FL funding"]},
    ],
    "8062": [  # General Medical & Surgical Hospitals
        {"name": "Ascension Health", "reasons": ["Largest nonprofit system", "140+ hospitals", "Standardization"]},
        {"name": "CommonSpirit Health", "reasons": ["Large Catholic system", "140+ hospitals", "Multi-state"]},
        {"name": "Kaiser Permanente", "reasons": ["Integrated health system", "39 hospitals", "Innovation leader"]},
    ],
}


@app.get("/api/briefs/{slug}/similar")
async def find_similar_briefs(
    slug: str,
    page: int = Query(default=1),
    exclude: str = Query(default=""),
):
    """Similar-account matching with pagination. Page 1 = static peers. Page 2+ = AI-generated."""
    # Find the current brief
    current_path = None
    for p in BRIEFS_DIR.glob(f"{slug}-*.json"):
        if any(x in p.name for x in ('.failed.', '.meta.', '.battlecard.', '.salesreport.', '.discovery.', '.coach.', '.product-selection.', '.notes.')):
            continue
        current_path = p
        break
    if not current_path or not current_path.exists():
        return JSONResponse({"error": "Brief not found"}, status_code=404)

    current = json.loads(current_path.read_text())
    cur_snap = current.get("snapshot", {})
    cur_name = cur_snap.get("name", slug).lower()
    cur_vertical = cur_snap.get("vertical", "")
    cur_entity = current.get("entity_type", "")
    cur_state = cur_snap.get("headquarters_state", "")
    cur_size_tier = _compute_size_tier(current)
    cur_sic = cur_snap.get("sic_code", "")

    # Parse exclude list
    exclude_names = set()
    if exclude:
        exclude_names = {n.strip().lower() for n in exclude.split("|") if n.strip()}

    # --- Page 2+: AI-generated suggestions ---
    if page > 1:
        return await _generate_more_peers(cur_vertical, cur_state, cur_name, page, exclude_names)

    # --- Page 1: Static matching ---

    # Part 1: Match existing briefs
    existing_names = {cur_name}
    results = []
    for p in sorted(BRIEFS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        if any(x in p.name for x in ('.failed.', '.meta.', '.battlecard.', '.salesreport.', '.discovery.', '.coach.', '.product-selection.', '.notes.')):
            continue
        other_slug = _extract_slug_from_stem(p.stem)
        if other_slug == slug:
            continue
        try:
            other = json.loads(p.read_text())
        except Exception:
            continue

        o_snap = other.get("snapshot", {})
        o_vertical = o_snap.get("vertical", "")
        o_name = o_snap.get("name", other_slug)
        existing_names.add(o_name.lower())

        if not cur_vertical or not o_vertical or cur_vertical.lower() != o_vertical.lower():
            continue

        score = 50
        reasons = [f"Same vertical: {cur_vertical}"]

        o_entity = other.get("entity_type", "")
        if cur_entity and o_entity and cur_entity.lower() == o_entity.lower():
            score += 20
            reasons.append(f"Same type: {cur_entity}")

        o_state = o_snap.get("headquarters_state", "")
        if cur_state and o_state and cur_state.lower() == o_state.lower():
            score += 25
            reasons.append(f"Same state: {cur_state}")

        o_size_tier = _compute_size_tier(other)
        if cur_size_tier == o_size_tier:
            score += 15
            reasons.append(f"Similar scale: {cur_size_tier}")

        o_sic = o_snap.get("sic_code", "")
        if cur_sic and o_sic and cur_sic == o_sic:
            score += 10
            reasons.append(f"Same SIC: {cur_sic}")

        results.append({
            "slug": other_slug,
            "company_name": o_name,
            "vertical": o_vertical,
            "score": min(score, 100),
            "match_reasons": reasons,
            "url": f"/brief/{other_slug}",
        })

    results.sort(key=lambda x: -x["score"])

    # Part 2: Static suggestions from peer knowledge base
    suggestions = []
    seen_names = set(existing_names)

    for v_key, peers in _VERTICAL_PEERS.items():
        if cur_vertical and cur_vertical.lower() == v_key.lower():
            for peer in peers:
                pname = peer["name"].lower()
                if pname not in seen_names:
                    seen_names.add(pname)
                    suggestions.append({
                        "company_name": peer["name"],
                        "match_reasons": peer["reasons"],
                        "source": "vertical",
                    })

    if cur_sic and cur_sic in _SIC_PEERS:
        sic_suggestions = []
        for peer in _SIC_PEERS[cur_sic]:
            pname = peer["name"].lower()
            if pname not in seen_names:
                seen_names.add(pname)
                sic_suggestions.append({
                    "company_name": peer["name"],
                    "match_reasons": peer["reasons"],
                    "source": "sic_code",
                })
        suggestions = sic_suggestions + suggestions

    return {
        "results": results[:5],
        "suggestions": suggestions[:10],
        "page": 1,
        "has_more": True,
    }


async def _generate_more_peers(vertical: str, state: str, company_name: str, page: int, exclude_names: set):
    """Generate additional peer suggestions via Haiku for pages 2+."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or page > 5:
        return {"results": [], "suggestions": [], "page": page, "has_more": False}

    v = vertical or "companies"
    state_hint = f" in or near {state}" if state else ""

    angles = [
        f"national {v} organizations with large facility footprints that would benefit from cloud-managed physical security",
        f"mid-size {v} organizations{state_hint} that are growing, expanding campuses, or modernizing infrastructure",
        f"{v} organizations with unique or high-value security needs — distribution centers, campuses, public-facing facilities",
        f"smaller or regional {v} organizations{state_hint} with multi-site operations and physical security budgets",
    ]
    angle = angles[(page - 2) % len(angles)]

    exclude_clause = ""
    if exclude_names:
        exclude_list = ", ".join(sorted(exclude_names)[:40])
        exclude_clause = f"\n\nDo NOT include any of these already-listed organizations: {exclude_list}"

    try:
        client = anthropic.Anthropic(timeout=20.0)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": (
                    f"List 8-10 real {v} organizations that would be strong prospects for "
                    f"Verkada physical security (cameras, access control, sensors). "
                    f"Focus on {angle}.{exclude_clause}\n\n"
                    f"For each, provide a brief reason why they'd be a good prospect.\n\n"
                    f"Return ONLY a JSON array: "
                    f'[{{"name": "Org Name", "reasons": ["reason1", "reason2"]}}]\n'
                    f"No markdown, no explanation, just the JSON array."
                ),
            }],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if text.startswith("["):
            items = json.loads(text)
            suggestions = []
            for item in items:
                if isinstance(item, dict) and item.get("name"):
                    if item["name"].lower() not in exclude_names:
                        suggestions.append({
                            "company_name": item["name"],
                            "match_reasons": item.get("reasons", []),
                            "source": "ai_suggestion",
                        })
            return {
                "results": [],
                "suggestions": suggestions[:10],
                "page": page,
                "has_more": page < 5 and len(suggestions) >= 5,
            }
    except Exception:
        pass
    return {"results": [], "suggestions": [], "page": page, "has_more": False}


# ---------------------------------------------------------------------------
# Batch research launcher
# ---------------------------------------------------------------------------

class BatchResearchRequest(BaseModel):
    accounts: list[str]

@app.post("/api/batch-research")
async def api_batch_research(req: BatchResearchRequest):
    results = []
    for company_name in req.accounts:
        slug = company_name.lower().replace(" ", "-").replace(",", "").replace(".", "")
        cmd = [sys.executable, str(PROJECT_ROOT / "orchestrator.py"), company_name]
        subprocess.Popen(
            cmd,
            env=os.environ.copy(),
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        results.append({"slug": slug, "company": company_name, "status_url": f"/status.html?slug={slug}"})
    return {"queued": results, "count": len(results)}




# ---------------------------------------------------------------------------
# Notes page
# ---------------------------------------------------------------------------

@app.get("/notes", response_class=HTMLResponse)
async def notes_page(slug: str = Query(...), date: str = Query(...)):
    if ".." in slug or ".." in date:
        return HTMLResponse("<h1>Invalid</h1>", status_code=400)
    brief_path, brief_data = find_valid_brief(slug, date)
    if not brief_data:
        return HTMLResponse("<h1>Brief not found</h1>", status_code=404)
    brief_name = brief_data.get("snapshot", {}).get("name", slug)
    template = _jinja_env.get_template("notes.html")
    return template.render(slug=slug, date=date, brief_name=brief_name)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "briefs": len(list(BRIEFS_DIR.glob("*.json")))}


# ---------------------------------------------------------------------------
# Nearby company search — location-based vertical search
# ---------------------------------------------------------------------------

# Top US metro areas for autocomplete (city, state abbreviation)
_US_METROS: list[dict] = [
    {"city": "Atlanta", "state": "GA"}, {"city": "Austin", "state": "TX"}, {"city": "Baltimore", "state": "MD"},
    {"city": "Birmingham", "state": "AL"}, {"city": "Boise", "state": "ID"}, {"city": "Boston", "state": "MA"},
    {"city": "Buffalo", "state": "NY"}, {"city": "Charlotte", "state": "NC"}, {"city": "Chicago", "state": "IL"},
    {"city": "Cincinnati", "state": "OH"}, {"city": "Cleveland", "state": "OH"}, {"city": "Columbus", "state": "OH"},
    {"city": "Dallas", "state": "TX"}, {"city": "Denver", "state": "CO"}, {"city": "Des Moines", "state": "IA"},
    {"city": "Detroit", "state": "MI"}, {"city": "El Paso", "state": "TX"}, {"city": "Fort Worth", "state": "TX"},
    {"city": "Fresno", "state": "CA"}, {"city": "Grand Rapids", "state": "MI"}, {"city": "Greensboro", "state": "NC"},
    {"city": "Hartford", "state": "CT"}, {"city": "Honolulu", "state": "HI"}, {"city": "Houston", "state": "TX"},
    {"city": "Indianapolis", "state": "IN"}, {"city": "Jacksonville", "state": "FL"}, {"city": "Kansas City", "state": "MO"},
    {"city": "Knoxville", "state": "TN"}, {"city": "Las Vegas", "state": "NV"}, {"city": "Little Rock", "state": "AR"},
    {"city": "Los Angeles", "state": "CA"}, {"city": "Louisville", "state": "KY"}, {"city": "Memphis", "state": "TN"},
    {"city": "Miami", "state": "FL"}, {"city": "Milwaukee", "state": "WI"}, {"city": "Minneapolis", "state": "MN"},
    {"city": "Nashville", "state": "TN"}, {"city": "New Orleans", "state": "LA"}, {"city": "New York", "state": "NY"},
    {"city": "Newark", "state": "NJ"}, {"city": "Norfolk", "state": "VA"}, {"city": "Oakland", "state": "CA"},
    {"city": "Oklahoma City", "state": "OK"}, {"city": "Omaha", "state": "NE"}, {"city": "Orlando", "state": "FL"},
    {"city": "Philadelphia", "state": "PA"}, {"city": "Phoenix", "state": "AZ"}, {"city": "Pittsburgh", "state": "PA"},
    {"city": "Portland", "state": "OR"}, {"city": "Providence", "state": "RI"}, {"city": "Raleigh", "state": "NC"},
    {"city": "Richmond", "state": "VA"}, {"city": "Riverside", "state": "CA"}, {"city": "Rochester", "state": "NY"},
    {"city": "Sacramento", "state": "CA"}, {"city": "Salt Lake City", "state": "UT"}, {"city": "San Antonio", "state": "TX"},
    {"city": "San Diego", "state": "CA"}, {"city": "San Francisco", "state": "CA"}, {"city": "San Jose", "state": "CA"},
    {"city": "Seattle", "state": "WA"}, {"city": "St. Louis", "state": "MO"}, {"city": "Tampa", "state": "FL"},
    {"city": "Tucson", "state": "AZ"}, {"city": "Tulsa", "state": "OK"}, {"city": "Virginia Beach", "state": "VA"},
    {"city": "Washington", "state": "DC"}, {"city": "Albuquerque", "state": "NM"}, {"city": "Anchorage", "state": "AK"},
    {"city": "Bakersfield", "state": "CA"}, {"city": "Baton Rouge", "state": "LA"}, {"city": "Charleston", "state": "SC"},
    {"city": "Colorado Springs", "state": "CO"}, {"city": "Columbia", "state": "SC"}, {"city": "Dayton", "state": "OH"},
    {"city": "Durham", "state": "NC"}, {"city": "Greenville", "state": "SC"}, {"city": "Huntsville", "state": "AL"},
    {"city": "Jackson", "state": "MS"}, {"city": "Lexington", "state": "KY"}, {"city": "Lincoln", "state": "NE"},
    {"city": "Madison", "state": "WI"}, {"city": "McAllen", "state": "TX"}, {"city": "Savannah", "state": "GA"},
    {"city": "Spokane", "state": "WA"}, {"city": "Syracuse", "state": "NY"}, {"city": "Tallahassee", "state": "FL"},
    {"city": "Toledo", "state": "OH"}, {"city": "Wichita", "state": "KS"}, {"city": "Winston-Salem", "state": "NC"},
    {"city": "Marietta", "state": "GA"}, {"city": "Decatur", "state": "GA"}, {"city": "Sandy Springs", "state": "GA"},
    {"city": "Alpharetta", "state": "GA"}, {"city": "Roswell", "state": "GA"}, {"city": "Kennesaw", "state": "GA"},
    {"city": "Duluth", "state": "GA"}, {"city": "Lawrenceville", "state": "GA"}, {"city": "Peachtree City", "state": "GA"},
    {"city": "Stockbridge", "state": "GA"}, {"city": "Macon", "state": "GA"}, {"city": "Augusta", "state": "GA"},
    {"city": "Athens", "state": "GA"}, {"city": "Valdosta", "state": "GA"}, {"city": "Columbus", "state": "GA"},
]

# Vertical → search query templates for nearby search
_VERTICAL_SEARCH_QUERIES: dict[str, list[str]] = {
    "retail": [
        '"{vertical}" retail stores near {location} within {radius} miles',
        'largest retailers headquarters near {location}',
    ],
    "k-12": [
        'school districts near {location}',
        'largest public school districts {state}',
    ],
    "healthcare": [
        'hospitals health systems near {location}',
        'largest healthcare providers {location} area',
    ],
    "higher ed": [
        'colleges universities near {location}',
        'largest university campuses {state}',
    ],
    "manufacturing": [
        'manufacturing plants factories near {location}',
        'largest manufacturers {location} area',
    ],
    "government": [
        'government offices agencies near {location}',
        'city county government {location}',
    ],
    "senior living": [
        'senior living communities near {location}',
        'assisted living facilities {location} area',
    ],
    "hospitality": [
        'hotels resorts near {location}',
        'largest hospitality companies {location} area',
    ],
    "commercial real estate": [
        'commercial real estate companies near {location}',
        'office building management {location}',
    ],
    "critical infrastructure": [
        'utility companies power plants near {location}',
        'data centers warehouses {location} area',
    ],
}


@app.get("/api/cities/suggest")
async def suggest_cities(q: str = Query(default="")):
    """Return matching US metro areas for autocomplete."""
    if not q or len(q) < 2:
        return {"suggestions": []}
    q_lower = q.lower()
    matches = []
    for m in _US_METROS:
        label = f"{m['city']}, {m['state']}"
        # Match on city name or state
        if q_lower in m["city"].lower() or q_lower in m["state"].lower() or q_lower in label.lower():
            matches.append({"label": label, "city": m["city"], "state": m["state"]})
            if len(matches) >= 8:
                break
    return {"suggestions": matches}


@app.get("/api/search/nearby")
async def search_nearby(
    vertical: str = Query(default=""),
    location: str = Query(default=""),
    radius: int = Query(default=50),
    page: int = Query(default=1),
    exclude: str = Query(default=""),
):
    """Search for companies in a vertical near a location. Supports pagination via page + exclude."""
    if not location:
        return JSONResponse({"error": "Location is required"}, status_code=400)

    # Parse exclude list (comma-separated company names already shown)
    exclude_names = set()
    if exclude:
        exclude_names = {n.strip().lower() for n in exclude.split("|") if n.strip()}

    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    if not tavily_key:
        return await _fallback_nearby_search(vertical, location, radius, page, exclude_names)

    # Build search queries
    v_lower = (vertical or "").lower()
    state = ""
    if "," in location:
        parts = location.split(",")
        state = parts[-1].strip()

    templates = _VERTICAL_SEARCH_QUERIES.get(v_lower, [
        '{vertical} companies near {location} within {radius} miles',
    ])

    # Run up to 2 Tavily searches
    all_results = []
    seen_titles = set()

    async with httpx.AsyncClient(timeout=15.0) as client:
        for tmpl in templates[:2]:
            query = tmpl.format(
                vertical=vertical or "companies",
                location=location,
                radius=radius,
                state=state or location,
            )
            try:
                r = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": tavily_key,
                        "query": query,
                        "max_results": 8,
                        "search_depth": "basic",
                        "include_answer": False,
                        "include_raw_content": False,
                    },
                )
                if r.status_code == 200:
                    data = r.json()
                    for result in data.get("results", []):
                        title = result.get("title", "")
                        if title and title not in seen_titles:
                            seen_titles.add(title)
                            all_results.append(result)
            except Exception:
                continue

    # Extract company names from search results — try AI extraction first, fall back to heuristics
    companies = await _extract_companies_ai(all_results, vertical, location)
    if not companies:
        companies = _extract_companies_from_search(all_results, vertical, location)

    # Filter out excludes
    companies = [c for c in companies if c["company_name"].lower() not in exclude_names]

    return {"results": companies, "location": location, "vertical": vertical, "radius": radius, "page": page, "has_more": True}


async def _extract_companies_ai(results: list[dict], vertical: str, location: str) -> list[dict]:
    """Use Haiku to extract company names from search results."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not results:
        return []
    try:
        snippets = "\n".join(
            f"- Title: {r.get('title', '')} | Snippet: {r.get('content', '')[:200]}"
            for r in results[:15]
        )
        client = anthropic.Anthropic(timeout=15.0)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": f"Extract specific company/organization names from these search results about {vertical} near {location}. Return ONLY a JSON array of objects with 'name' and 'context' (one-line description). No markdown, no explanation. Max 10 companies. Skip generic list pages.\n\n{snippets}",
            }],
        )
        text = resp.content[0].text.strip()
        # Parse JSON array
        if text.startswith("["):
            items = json.loads(text)
            return [
                {"company_name": item["name"], "context": item.get("context", ""), "source": "search"}
                for item in items if isinstance(item, dict) and item.get("name")
            ][:10]
    except Exception:
        pass
    return []


def _extract_companies_from_search(results: list[dict], vertical: str, location: str) -> list[dict]:
    """Extract company names and context from Tavily search results."""
    companies = []
    seen = set()

    for r in results:
        title = r.get("title", "")
        content = r.get("content", "")
        url = r.get("url", "")

        # Clean title: remove "- Wikipedia", "| ...", "... - ..." suffixes
        clean_title = title.split(" - ")[0].split(" | ")[0].split(" — ")[0].strip()

        # Skip generic/list pages
        skip_words = ["best", "top ", "list of", "ranking", "review", "yelp", "indeed", "glassdoor", "map", "directory"]
        if any(w in title.lower() for w in skip_words):
            # But still try to extract names from the content snippet
            pass
        elif clean_title and clean_title.lower() not in seen and len(clean_title) < 80:
            seen.add(clean_title.lower())
            companies.append({
                "company_name": clean_title,
                "context": content[:200] if content else "",
                "source_url": url,
                "source": "search",
            })

    return companies[:12]


async def _fallback_nearby_search(vertical: str, location: str, radius: int, page: int = 1, exclude_names: set = None):
    """Fallback when Tavily is unavailable — use Haiku to list companies in the area."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"results": [], "location": location, "vertical": vertical, "radius": radius,
                "page": page, "has_more": False,
                "error": "No search API configured. Set TAVILY_API_KEY or ANTHROPIC_API_KEY."}

    v = vertical or "companies"
    exclude_names = exclude_names or set()

    # Build exclude clause for Haiku
    exclude_clause = ""
    if exclude_names:
        exclude_list = ", ".join(sorted(exclude_names)[:30])
        exclude_clause = f"\n\nDo NOT include any of these already-listed companies: {exclude_list}"

    # Vary the prompt angle per page to get diverse results
    page_angles = [
        "large or notable organizations that would be good Verkada physical security prospects (multi-site, significant facilities, security needs)",
        "mid-size organizations with growing campuses, new construction, or recent security incidents — the kind of accounts an SE would prospect",
        "organizations with unique security needs — warehouses, distribution centers, data centers, mixed-use campuses, or facilities open to the public",
        "smaller but fast-growing organizations expanding facilities, or niche players with high-value assets needing physical security",
        "public-sector organizations, nonprofits, religious institutions, or community organizations with physical security needs",
    ]
    angle = page_angles[(page - 1) % len(page_angles)]

    # Stop after 5 pages
    if page > 5:
        return {"results": [], "location": location, "vertical": vertical, "radius": radius,
                "page": page, "has_more": False}

    try:
        client = anthropic.Anthropic(timeout=20.0)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": (
                    f"List 8-10 real {v} organizations/companies headquartered or with major "
                    f"facilities within {radius} miles of {location}. Focus on {angle}.{exclude_clause}\n\n"
                    f"Return ONLY a JSON array of objects: "
                    f'[{{"name": "Company Name", "context": "Brief 1-line description with location detail"}}]\n'
                    f"No markdown, no explanation, just the JSON array."
                ),
            }],
        )
        text = resp.content[0].text.strip()
        # Strip markdown fencing if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if text.startswith("["):
            items = json.loads(text)
            # Filter out excludes (in case Haiku ignores the instruction)
            results = []
            for item in items:
                if isinstance(item, dict) and item.get("name"):
                    if item["name"].lower() not in exclude_names:
                        results.append({
                            "company_name": item["name"],
                            "context": item.get("context", ""),
                            "source": "ai_suggestion",
                        })
            return {
                "results": results[:10],
                "location": location, "vertical": vertical, "radius": radius,
                "page": page, "has_more": page < 5 and len(results) >= 5,
            }
        return {"results": [], "location": location, "vertical": vertical, "radius": radius,
                "page": page, "has_more": False, "error": "Search returned unexpected format"}
    except Exception as e:
        return {"results": [], "location": location, "vertical": vertical, "radius": radius,
                "page": page, "has_more": False, "error": f"Search failed: {str(e)[:100]}"}


# ---------------------------------------------------------------------------
# Autocomplete suggestions
# ---------------------------------------------------------------------------

@app.get("/api/suggest")
async def suggest(q: str = Query(default="")):
    """Return autocomplete suggestions from Google's suggestion API."""
    if not q or len(q) < 2:
        return {"suggestions": []}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(
                "https://suggestqueries.google.com/complete/search",
                params={"client": "firefox", "q": q},
            )
            data = r.json()
            raw = data[1] if len(data) > 1 else []
            suggestions = [s for s in raw if len(s) <= 60][:8]
            return {"suggestions": suggestions}
    except Exception:
        return {"suggestions": []}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_cleanup():
    moved = archive_failed_briefs()
    if moved:
        print(f"  Archived {moved} failed brief(s) to briefs/archive/", flush=True)


if __name__ == "__main__":
    import uvicorn
    print(f"Chat server starting on http://localhost:{PORT}")
    print(f"Briefs directory: {BRIEFS_DIR}")
    valid = [p.name for p in BRIEFS_DIR.glob('*.json')
             if not any(x in p.name for x in ('.failed.', '.meta.', '.battlecard.', '.salesreport.', '.discovery.', '.coach.', '.product-selection.'))]
    print(f"Available briefs: {valid}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
