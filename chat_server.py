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

import json
import os
import sys
from pathlib import Path

import anthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
BRIEFS_DIR = PROJECT_ROOT / "briefs"
SONNET_MODEL = "claude-sonnet-4-6"
PORT = int(os.environ.get("PORT", 8000))

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
    brief_path = BRIEFS_DIR / f"{req.slug}-{req.date}.json"
    if not brief_path.exists():
        return {"error": f"Brief not found: {brief_path.name}"}

    brief_data = json.loads(brief_path.read_text())
    company_name = brief_data.get("snapshot", {}).get("name", req.slug)

    system_prompt = SYSTEM_TEMPLATE.format(
        company_name=company_name,
        brief_json=json.dumps(brief_data, indent=2, default=str),
    )

    messages = [{"role": m.role, "content": m.content} for m in req.history]
    messages.append({"role": "user", "content": req.message})

    client = anthropic.Anthropic()

    def generate():
        with client.messages.stream(
            model=SONNET_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'text': text})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


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
