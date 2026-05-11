#!/usr/bin/env python3
"""
Chat server — conversational interface grounded in brief data.

POST /chat with {slug, date, message, history[]}
Loads briefs/{slug}-{date}.json, builds system prompt with full brief context,
streams response from Claude Sonnet via Anthropic API.

Usage:
    python3 chat_server.py                  # localhost:8000
    PORT=9000 python3 chat_server.py        # custom port
"""

import glob as globmod
import json
import os
import sys
from pathlib import Path

import anthropic
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
BRIEFS_DIR = PROJECT_ROOT / "briefs"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
SONNET_MODEL = "claude-sonnet-4-6"
PORT = int(os.environ.get("PORT", 8000))
STATUS_DIR = Path("/tmp")

app = FastAPI(title="OSINT Chat", version="1.0")

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
