---
name: research
description: Full pipeline — collect sources, synthesize, render HTML brief
arguments:
  - name: company
    description: Company or organization name to research
    required: true
---

Run the full /research pipeline for "$ARGUMENTS.company".

Execute:
```
python3 orchestrator.py "$ARGUMENTS.company"
```

This runs all 4 phases:
1. **Phase 1 — Source Data Collection**: 14 clients in parallel (sec, indeed, crtsh, github, news, nces, clery, sam, sourcewell, tips, hhs, reddit, ga_procurement, atlanta_procurement). Each respects its own cache TTL. Failures logged but do not abort.
2. **Phase 2 — Subagent Synthesis**: company-bg, tech-and-pain, hiring-signals run in parallel (Sonnet). Output to sources/{slug}/.
3. **Phase 3 — Synthesizer**: Single Opus invocation combining all subagent outputs + persona/verkada-se.yml. Output to briefs/{slug}-{date}.json.
4. **Phase 4 — Render**: JSON → HTML via render/render.py using templates/brief.html. Output to briefs/{slug}-{date}.html.

Add `--force` to bust all caches. Add `--open` to open the rendered HTML in a browser.

Show the user the phase 1 status grid, phase 2/3/4 logs, runtime, and path to rendered HTML.
