#!/usr/bin/env python3
"""Renders a brief JSON file to HTML using the Jinja2 template."""

import json
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Undefined


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "templates"
BRIEFS_DIR = PROJECT_ROOT / "briefs"


def confidence_badge(confidence: str) -> str:
    colors = {
        "high": "bg-green-100 text-green-700 border-green-200",
        "medium": "bg-amber-100 text-amber-700 border-amber-200",
        "inference": "bg-red-100 text-red-700 border-red-200",
    }
    css = colors.get(confidence, "bg-gray-100 text-gray-600 border-gray-200")
    return f'<span class="badge inline-block px-1.5 py-0.5 rounded border {css} uppercase font-semibold">{confidence}</span>'


def quality_badge(quality: str) -> str:
    if not quality:
        return ""
    colors = {
        "primary": "bg-blue-50 text-blue-600 border-blue-200",
        "secondary": "bg-gray-50 text-gray-600 border-gray-200",
        "weak": "bg-red-50 text-red-600 border-red-200",
    }
    css = colors.get(quality, "bg-gray-50 text-gray-500 border-gray-200")
    return f'<span class="badge inline-block px-1.5 py-0.5 rounded border {css} uppercase font-semibold">{quality}</span>'


def source_chip(source: dict) -> str:
    if not source:
        return ""
    url = source.get("url", "#")
    title = source.get("title", "source")
    retrieved = source.get("retrieved_at", "")
    date_part = retrieved[:10] if retrieved else ""
    # Truncate title for display
    display = title[:60] + "..." if len(title) > 60 else title
    return (
        f'<a href="{url}" target="_blank" rel="noopener" '
        f'class="source-chip inline-flex items-center gap-1 px-2 py-0.5 rounded-full '
        f'bg-gray-100 text-gray-500 hover:bg-blue-100 hover:text-blue-700 transition-colors" '
        f'title="{title}">'
        f'<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        f'<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" '
        f'd="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/>'
        f'</svg>'
        f'{display}'
        f'{"  " + date_part if date_part else ""}'
        f'</a>'
    )


class _SilentUndefined(Undefined):
    """Return empty string / falsy for missing attributes instead of raising."""
    def __str__(self):
        return ""
    def __iter__(self):
        return iter([])
    def __bool__(self):
        return False
    def __getattr__(self, name):
        return self


def render_brief(json_path: Path) -> Path:
    with open(json_path) as f:
        data = json.load(f)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
        undefined=_SilentUndefined,
    )
    env.globals["confidence_badge"] = confidence_badge
    env.globals["quality_badge"] = quality_badge
    env.globals["source_chip"] = source_chip

    template = env.get_template("brief.html")
    html = template.render(data=data)

    out_path = json_path.with_suffix(".html")
    out_path.write_text(html)
    return out_path


def main():
    if len(sys.argv) < 2:
        # Find most recent brief JSON
        jsons = sorted(BRIEFS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not jsons:
            print("Usage: render.py <brief.json>", file=sys.stderr)
            print("No brief JSON files found in briefs/", file=sys.stderr)
            sys.exit(1)
        json_path = jsons[0]
    else:
        json_path = Path(sys.argv[1])

    if not json_path.exists():
        print(f"File not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    out = render_brief(json_path)
    print(f"Rendered: {out}")


if __name__ == "__main__":
    main()
