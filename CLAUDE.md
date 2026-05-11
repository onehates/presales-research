# CLAUDE.md

Project-level instructions for Claude Code. Read this fully before responding to any request in this repo.

---

## Project

**Multi-agent AI pre-sales research platform**, built for use by a Verkada Solutions Engineer. Takes a target account as input. Produces a polished HTML discovery brief and a suite of related sales-support artifacts. Output quality is judged on **specificity and source attribution**, not breadth or polish-of-prose.

The tool's value is *not* "AI generates research." Generic LLMs already do that. The tool's value is the persona-file rule engine in `persona/verkada-se.yml`, which forces output to be Verkada-specific and SE-grade. **Any code or content you generate must reinforce that specificity, never dilute it.**

---

## Slash Command Surface (V1)

| Command | Purpose | Reads From |
|---|---|---|
| `/research <company>` | Full pipeline: dispatch subagents, synthesize, render HTML | All sources |
| `/displace <company>` | Competitive displacement battle card | Cached brief + persona |
| `/vertical-lens <company>` | Vertical-specific angle overlay (K-12 / healthcare / etc.) | Cached brief + persona |
| `/discovery-prep <company> <persona>` | Persona-tuned discovery questions | Cached brief + persona |
| `/demo-plan <company>` | Recommended Command modules + 30-min demo script | Cached brief + persona |
| `/size <company>` | Rough product mix from sites/doors/cameras inputs | Cached brief + persona |
| `/followup <call_notes>` | Post-call email drafter | Persona only |

`/research` is the only command that runs the full data-collection pipeline. All other commands read from `sources/<company>/` cache plus `briefs/<company>-<latest>.json`. **If `/displace` or any other module triggers fresh data collection, that's a bug.**

---

## File Layout

```
.claude/
  agents/         Subagent definitions (one .md per subagent)
  commands/       Slash command definitions (one .md per command)
  settings.json   Tool permissions, MCP servers
persona/
  verkada-se.yml  THE LEVERAGE FILE. Read before any synthesis task.
sources/
  <company>/      Per-company raw data cache (JSON files)
templates/
  brief.html      HTML template, Tailwind CDN, single file
  partials/       Per-module HTML partials (displace.html, etc.)
render/
  render.py       JSON → HTML interpolation
briefs/
  <company>-<date>.html   Final deliverables
  <company>-<date>.json   Structured brief data (read by other modules)
```

---

## The Persona File (Read First, Always)

`persona/verkada-se.yml` is a structured rule engine, not a description doc. It contains:

- `product` — Verkada's product lines and positioning
- `icp` — verticals with key drivers
- `displacement_targets` — incumbent vendors with common pain themes
- `triggers` — detection signals → discovery question templates, with weights
- `personas` — buyer personas with `care_about` and `skip_topics`
- `disqualifiers` — flags that should kill an opportunity

**Any synthesis task must load this file and use it as ground truth for:**
- Which signals matter (and how much)
- Which discovery questions to surface
- Which topics to skip per persona
- Which disqualifiers to flag

**Do not invent triggers, personas, or templates that aren't in this file.** If you find a gap, surface it as a comment in your output, do not fill it silently.

---

## Model Assignment (Strict)

| Task | Model |
|---|---|
| Source parsing, extraction, deduplication | Haiku |
| Subagent reasoning (per-source synthesis) | Sonnet |
| Final synthesizer (assembles brief) | Sonnet + extended thinking |
| Specificity rewrite pass | Sonnet + extended thinking |
| Module commands (`/displace`, `/discovery-prep`, etc.) | Sonnet |

Do not promote tasks to Opus to "improve quality" — output quality comes from prompts and the persona file, not from model size at the wrong layer.

---

## Anti-Genericness Rules (MANDATORY for all agents)

These are non-negotiable. Any agent or module you create or modify must enforce them:

1. **Source attribution required.** Every factual claim outputs `[source_url, retrieved_date]`. Claims without sources are marked `[INFERENCE]` or dropped.
2. **`insufficient_data` is a valid output.** Empty sections beat filler. If a section can't be supported by ≥2 sources, output: `insufficient data — would require: <specific source name>`.
3. **No generic claims.** Reject any sentence that could appear unchanged in a brief about a different company in the same industry. Run a specificity rewrite pass on every synthesis.
4. **Confidence levels per claim.** Every claim is tagged `high` / `medium` / `inference`.
5. **Trigger-driven discovery questions.** Match detected signals → `triggers` in `verkada-se.yml` → templates → fill with brief specifics. Do not free-form generate discovery questions.
6. **No hedging language as filler.** "Likely," "potentially," "may have" are only acceptable when paired with a confidence tag and a missing-source explanation.

---

## Output Format

- Final deliverable is **HTML, rendered in browser**. Not markdown.
- Synthesizer outputs JSON matching the schema in `templates/brief.schema.json`. The render step interpolates JSON → HTML.
- Visual language is Verkada-adjacent: clean whites, structured cards, quiet color accents. Tailwind CDN, no build step.
- Every claim in the HTML has a source footnote chip linking to the source URL.
- Confidence is rendered as color-coded badges (green=sourced, yellow=inferred, red=speculation).
- TL;DR card at top: 3 bullets max, 30-second skim.

---

## Caching Rules

- All raw source data goes to `sources/<company>/<source>.json` with a `retrieved_at` timestamp at the top of each file.
- Cache TTLs: SEC filings 90 days, news 7 days, job postings 14 days, GitHub 30 days.
- `/research` re-fetches sources whose cache is stale. Other commands NEVER fetch — they fail loudly if cache is missing.
- The brief JSON in `briefs/<company>-<date>.json` is the contract between `/research` and all other modules. Do not break this schema.

---

## Adding a New Agent

When asked to add a subagent:

1. Create `.claude/agents/<name>.md` with: system prompt, assigned source(s), tool allowlist (only what's needed), output JSON schema, explicit failure mode (`{status: "insufficient_data"}`).
2. Subagent is constrained to a single source category. Don't build "research everything" agents.
3. Include negative few-shots in the system prompt (one GENERIC labeled example, one SPECIFIC labeled example).
4. Update this file's "Subagent Roster" if you're adding to V1 surface.

---

## Adding a New Command

When asked to add a slash command:

1. Create `.claude/commands/<name>.md` with: invocation pattern, agents to dispatch, inputs read, outputs written.
2. Default to reading from cache. Only `/research` fetches new data.
3. Add an HTML partial in `templates/partials/<name>.html` if the command produces visual output.
4. Confirm the command appears in the slash command surface table above before considering it done.

---

## Failure Modes (Do NOT)

- Do not generate filler prose to make a section "feel complete." Output `insufficient_data` instead.
- Do not invent companies, leadership names, dates, or specific dollar figures. If a number isn't in a source, it doesn't exist.
- Do not generalize discovery questions. If a question doesn't tie to a specific trigger that fired, drop it.
- Do not promote a Sonnet task to Opus to "make it better." Fix the prompt or the persona file instead.
- Do not have non-`/research` commands trigger live web fetches. They read cache only.
- Do not bypass source attribution because it's "inconvenient" for layout. The footnote chips ARE the layout.
- Do not use `claude.ai/chat`-style chatty preambles in agent outputs. Agents output structured JSON. The render layer makes it human.

---

## Demo Context (Operational Note)

This project's first delivery context is a **Verkada SE final on-site interview**. Two pre-cached demo accounts live in `sources/`. The persona file is tuned to make those two accounts' briefs especially strong. When iterating prompts or persona content, the question to ask is always: **"Does this make the demo accounts' briefs more specific?"** If not, it's noise.

After the interview, the demo-account constraint relaxes. Until then, treat it as a project invariant.

---

## When in Doubt

If you're unsure whether to add complexity, the answer is no. The tool's leverage comes from the persona file, source attribution, and the synthesizer's anti-genericness pass. Everything else is plumbing. Resist the urge to make the plumbing fancy.
