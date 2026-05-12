#!/usr/bin/env python3
"""Renders a brief JSON file to HTML using the Jinja2 template."""

import json
import re
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, Undefined


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "templates"
BRIEFS_DIR = PROJECT_ROOT / "briefs"
PERSONA_PATH = PROJECT_ROOT / "persona" / "verkada-se.yml"
GLOSSARY_PATH = PROJECT_ROOT / "data" / "glossary.yml"


SPECIAL_CASES = {
    'k12': 'K-12',
    'k12_district': 'K-12 District',
    'higher_ed': 'Higher Ed',
    'state_local_gov': 'State & Local Gov',
    'public_safety': 'Public Safety',
    'critical_infrastructure': 'Critical Infrastructure',
    'senior_living': 'Senior Living',
    'meddic': 'MEDDIC',
    'gtm': 'GTM',
    'ndaa': 'NDAA',
    'frpl': 'FRPL',
    'sled': 'SLED',
    'ferpa': 'FERPA',
    'hipaa': 'HIPAA',
    'poc': 'POC',
    'nvr': 'NVR',
    'dvr': 'DVR',
    'it': 'IT',
    'federal_funding_ndaa': 'Federal Funding (NDAA)',
    'federal_funding_NDAA': 'Federal Funding (NDAA)',
}


def humanize_id(s: str) -> str:
    """Convert snake_case ID to human-readable title case with special cases."""
    if not s or not isinstance(s, str):
        return str(s) if s else ""
    lower = s.lower()
    if lower in SPECIAL_CASES:
        return SPECIAL_CASES[lower]
    if s in SPECIAL_CASES:
        return SPECIAL_CASES[s]
    return s.replace('_', ' ').title()


def confidence_badge(confidence: str) -> str:
    colors = {
        "high": "bg-green-100 text-green-700 border-green-200 dark:bg-green-950 dark:text-green-300 dark:border-green-800",
        "medium": "bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-800",
        "inference": "bg-red-100 text-red-700 border-red-200 dark:bg-red-950 dark:text-red-300 dark:border-red-800",
    }
    tooltips = {
        "high": "High confidence — directly sourced from primary documents (filings, official sites, named individuals)",
        "medium": "Medium confidence — sourced but inferred or aggregated from multiple secondary signals",
        "inference": "Inference — pattern-based deduction without direct evidence. Must validate via discovery",
    }
    css = colors.get(confidence, "bg-gray-100 text-gray-600 border-gray-200 dark:bg-slate-800 dark:text-slate-400 dark:border-slate-600")
    tip = tooltips.get(confidence, "")
    return f'<span class="badge inline-block px-1.5 py-0.5 rounded border {css} uppercase font-semibold" data-tooltip="{tip}">{confidence}</span>'


def quality_badge(quality: str) -> str:
    if not quality:
        return ""
    colors = {
        "primary": "bg-blue-50 text-blue-600 border-blue-200 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-800",
        "secondary": "bg-gray-50 text-gray-600 border-gray-200 dark:bg-slate-800 dark:text-slate-400 dark:border-slate-600",
        "weak": "bg-red-50 text-red-600 border-red-200 dark:bg-red-950 dark:text-red-300 dark:border-red-800",
    }
    tooltips = {
        "primary": "Primary source — official company filings, primary government sites, direct citations",
        "secondary": "Secondary source — news articles, third-party reports, expert blogs",
        "weak": "Weak source — Reddit threads, forums, unverified social media",
    }
    css = colors.get(quality, "bg-gray-50 text-gray-500 border-gray-200 dark:bg-slate-800 dark:text-slate-400 dark:border-slate-600")
    tip = tooltips.get(quality, "")
    return f'<span class="badge inline-block px-1.5 py-0.5 rounded border {css} uppercase font-semibold" data-tooltip="{tip}">{quality}</span>'


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
        f'bg-gray-100 text-gray-500 hover:bg-blue-100 hover:text-blue-700 '
        f'dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-blue-950 dark:hover:text-blue-300 transition-colors" '
        f'title="{title}">'
        f'<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        f'<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" '
        f'd="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/>'
        f'</svg>'
        f'{display}'
        f'{"  " + date_part if date_part else ""}'
        f'</a>'
    )


# ---------------------------------------------------------------------------
# Internal term sanitization — clean agent internals from user-facing prose
# ---------------------------------------------------------------------------

# Compiled list of (pattern, replacement) pairs applied to all string values
# in the brief JSON before rendering. Order matters: more specific patterns first.
_INTERNAL_TERM_REPLACEMENTS = [
    # Internal file references
    (re.compile(r'The \w+\.json (?:file )?contains?', re.IGNORECASE), 'The available data shows'),
    (re.compile(r'\b\w+\.json\b'), ''),

    # Internal config keys / agent internals
    (re.compile(r'\bhaiku_analysis\b'), 'preliminary analysis'),
    (re.compile(r'\bhaiku_classification\b'), 'classification'),
    (re.compile(r'\btrigger_matches\b'), 'signal matches'),
    (re.compile(r'\bdetect_signals\.[a-z_]+'), 'persona signals'),
    (re.compile(r'\bdetect_signals\b'), 'persona signals'),
    (re.compile(r'\bjob_titles\b'), 'job title patterns'),
    (re.compile(r'\bdiscovery_templates\b'), 'discovery templates'),
    (re.compile(r'\bleverage_references\b'), 'customer references'),
    (re.compile(r'\bsynthesis_error\b'), 'synthesis issue'),
    (re.compile(r'\bsubagent_outputs?\b'), 'analysis output'),
    (re.compile(r'\bsubagents?\b'), 'analysis step'),
    (re.compile(r'\bphase\s*[0-3]\b', re.IGNORECASE), 'analysis phase'),

    # "Entry 1 / Match 1 / Result 1" patterns (debug numbering)
    (re.compile(r'\bEntry\s+\d+:?\s*'), ''),
    (re.compile(r'\bMatch\s+\d+:?\s*'), ''),
    (re.compile(r'\bResult\s+\d+:?\s*'), ''),

    # Internal coordinate references
    (re.compile(r'\bsection\.[a-z_\.]+\b'), 'section data'),
    (re.compile(r'\bdata\.[a-z_\.]+\b'), ''),

    # Debug-style prose that should never reach users
    (re.compile(r'\bare false positives?\.?'), 'are not relevant'),
    (re.compile(r'\bare discarded\.?'), 'are excluded'),
    (re.compile(r'\bare contextually invalid\b'), 'are not relevant'),
    (re.compile(r'\bfalse positives?\b'), 'non-matching results'),
    (re.compile(r'\bboilerplate\b'), 'standard'),
]


def _sanitize_string(s: str) -> str:
    """Apply internal term replacements to a single string."""
    for pattern, replacement in _INTERNAL_TERM_REPLACEMENTS:
        s = pattern.sub(replacement, s)
    # Clean up double spaces left by removals
    s = re.sub(r'  +', ' ', s).strip()
    return s


def _sanitize_data(obj):
    """Recursively sanitize all string values in a dict/list structure."""
    if isinstance(obj, str):
        return _sanitize_string(obj)
    elif isinstance(obj, dict):
        return {k: _sanitize_data(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_data(item) for item in obj]
    return obj


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


def render_battlecard(brief_data: dict, slug: str = "", date: str = "") -> str:
    """Render a battlecard HTML string from brief data."""
    brief_data = _sanitize_data(brief_data)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
        undefined=_SilentUndefined,
    )
    env.filters["humanize_id"] = humanize_id
    template = env.get_template("battlecard.html")
    return template.render(data=brief_data, slug=slug, date=date)


def render_brief(json_path: Path) -> Path:
    with open(json_path) as f:
        data = json.load(f)
    data = _sanitize_data(data)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
        undefined=_SilentUndefined,
    )
    env.globals["confidence_badge"] = confidence_badge
    env.globals["quality_badge"] = quality_badge
    env.globals["source_chip"] = source_chip
    env.filters["humanize_id"] = humanize_id

    # Load chat starter prompts from persona file
    chat_starter_prompts = []
    if PERSONA_PATH.exists():
        with open(PERSONA_PATH) as pf:
            persona = yaml.safe_load(pf)
            chat_starter_prompts = persona.get("chat_starter_prompts", [])

    # Load glossary terms
    glossary_terms = []
    if GLOSSARY_PATH.exists():
        with open(GLOSSARY_PATH) as gf:
            glossary_data = yaml.safe_load(gf)
            glossary_terms = glossary_data.get("terms", [])

    template = env.get_template("brief.html")
    html = template.render(data=data, chat_starter_prompts=chat_starter_prompts, glossary_terms=glossary_terms)

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
