# AI Pre-Sales Research Platform — Project Kickoff

**Owner:** Andy
**Context:** Verkada SE Final On-Site Interview — San Francisco
**Format:** Role-play sales demo (you sell, panel asks questions, live demo)
**Build window:** ~10 days

---

## The Project

A multi-agent AI research tool, built in Claude Code, that takes a target account and produces a polished HTML discovery brief: company background, technical indicators, pain signals, hiring signals, and persona-tuned discovery questions. V1 ships with the brief + discovery prep only. Multi-faceted modules (displacement, demo planner, etc.) are roadmap.

## Strategic Frame

**You are pitching the tool itself to Verkada SE leadership** during the role-play. The panel role-plays as the customer evaluating whether to adopt this for their SE org. This frame:

- Demonstrates SE skill (discovery, sell, demo, objection handling) using a real artifact
- Avoids the trap of pitching Verkada cameras back to Verkada SEs
- Positions you as already thinking about Verkada's productivity problems
- Turns "I built an AI thing" into a credible go-to-market motion

## Win Condition

Land the role. Two failure modes to design against:

1. Tool overshadows the candidate ("cool demo, unclear if he can sell")
2. Output reads generic ("seen better prototypes")

The second is the bigger risk. Most prep time goes into the **persona file** and **prompt engineering** — the things that drive output specificity — not the agent orchestration code.

---

## Core Architectural Decisions (Locked)

- **Stay in Claude Code.** Do NOT rewrite to Python. Streaming agent UI is part of the demo experience.
- **Output target: HTML, not markdown.** Polished HTML brief in browser is the deliverable.
- **Multi-agent in parallel:** 3 research subagents + synthesizer for V1.
- **Model assignment:** Haiku for extraction/parsing, Sonnet for subagents, Opus for synthesizer.
- **Source-specific clients, not generic WebSearch.** Each subagent hits assigned sources directly. WebSearch is fallback only.
- **Persona file as structured YAML, not bullet doc.** Synthesizer queries it as a rule engine.

---

## Project Structure

```
presales-research/
├── CLAUDE.md                          # Repo conventions, agent invocation rules
├── .claude/
│   ├── agents/
│   │   ├── company-bg.md
│   │   ├── tech-and-pain.md
│   │   ├── hiring-signals.md
│   │   └── synthesizer.md
│   ├── commands/
│   │   └── research.md                # /research <company> entry point
│   └── settings.json                  # Tool permissions
├── persona/
│   └── verkada-se.yml                 # The leverage file
├── sources/                           # Cached raw research data
│   └── <company>/
│       ├── sec.json
│       ├── jobs.json
│       ├── github.json
│       ├── news.json
│       └── ssl.json
├── templates/
│   └── brief.html                     # Tailwind HTML template
├── render/
│   └── render.{py,js}                 # JSON → HTML interpolation
└── briefs/
    └── <company>-<date>.html          # Final deliverables
```

---

## The Persona File (Day 1 Priority)

This file does 80% of the work. Without rigorous content here, the tool produces generic output regardless of architecture quality.

**Schema:**

```yaml
product:
  name: Verkada
  lines: [Cameras, Access Control, Alarms, Sensors, Intercoms, Guest, Mailroom, Workplace]
  unified_under: Command platform
  positioning: Cloud-managed physical security; replaces on-prem NVR/DVR + legacy access control

icp:
  verticals:
    - {name: K-12, key_drivers: [school_safety, weapon_detection, FERPA, federal_funding_NDAA]}
    - {name: Healthcare, key_drivers: [HIPAA, infant_abduction, ED_security, patient_elopement]}
    - {name: Retail, key_drivers: [ORC, shrink, ELM_integration]}
    - {name: Manufacturing, key_drivers: [worker_safety, plant_security, IT_OT_convergence]}
    - {name: HigherEd, key_drivers: [campus_safety, NDAA, dorm_access]}
    - {name: CRE_Hospitality, key_drivers: [tenant_experience, multi_tenant_access]}

displacement_targets:
  - {vendor: Avigilon, parent: Motorola, common_pain: [licensing_cost, on-prem_burden]}
  - {vendor: Genetec, common_pain: [on-prem_complexity, IT_burden]}
  - {vendor: Milestone, common_pain: [on-prem_burden, integration_cost]}
  - {vendor: Lenel, parent: Carrier, common_pain: [legacy_access_control, support]}
  - {vendor: Hikvision, common_pain: [NDAA_non-compliance]}
  - {vendor: Dahua, common_pain: [NDAA_non-compliance]}

triggers:
  - id: ndaa_compliance_pressure
    detect_signals:
      keywords: ["federal funding", "DoD contract", "Hikvision", "Dahua"]
    weight: 0.9
    discovery_templates:
      - "I noticed {company} receives federal funding through {program}. Are existing cameras NDAA-compliant, or has that come up in audits?"
      - "Hikvision/Dahua replacements are 2025 budget items for many {vertical} orgs. Where are you in that conversation?"

  - id: incident_recent_12mo
    detect_signals:
      keywords: ["breach", "incident", "lawsuit", "shooting", "violence", "theft"]
    weight: 1.0
    discovery_templates:
      - "I saw the {incident_type} coverage in {month}. How has that shifted security investment posture?"
      - "What did the post-incident review surface as the gap?"

  - id: hiring_security_intensity
    detect_signals:
      job_titles: ["Director of Security", "Security Operations", "Physical Security Manager"]
    weight: 0.8
    discovery_templates:
      - "I see you're scaling the security team — what's driving that?"

  - id: capital_project_signal
    detect_signals:
      keywords: ["new building", "expansion", "lease signed", "groundbreaking"]
    weight: 0.7
    discovery_templates:
      - "When does {project} come online? Security infrastructure decisions for that site already locked in?"

personas:
  IT_Director:
    care_about: [integration, IT_burden, cloud_strategy, vendor_consolidation]
    skip_topics: [door_hardware_specs, plant_floor_ops]
  Director_of_Facilities:
    care_about: [capital_projects, vendor_management, ops_cost]
    skip_topics: [SSO, API_integration]
  CSO:
    care_about: [risk_posture, incident_response, regulatory]
  Superintendent_K12:
    care_about: [student_safety, parent_communication, board_optics]

disqualifiers:
  - on-prem_only_mandate
  - existing_3yr_contract_just_signed
  - sub_50_employee_org
```

**Iterate this file harder than anything else in the repo.**

---

## Data Sources (Replace Generic WebSearch)

| Source | What it gives | Implementation |
|---|---|---|
| SEC EDGAR | 10-K risk factors, 10-Q forward statements, 8-K material events | Free API. Public companies only. Gold for pain signals. |
| Indeed Jobs | Hiring intensity, role specifics, tech stack signals | Free search. Scrape with rate limiting. |
| LinkedIn Jobs | Same as Indeed but richer | Phantombuster / Bright Data, or accept API limits |
| GitHub | Engineering culture, stack signals, scale | Free API. Org-level + recent activity. |
| Glassdoor | Employee pain signals (IT pain, security pain) | Scraping required. Sentiment + specific complaints. |
| crt.sh | SSL certs → subdomain enum → tech footprint | Free. Reveals SaaS, security tools, vendors. |
| PR Newswire / BusinessWire RSS | Press releases, cleaner than news | Free RSS. |
| Seeking Alpha | Earnings call transcripts | Public co's only. Scraping. |
| Google News | Backstop for general news | Free, generic results. |

**V1 minimum viable:** SEC EDGAR + Indeed + GitHub + crt.sh + Google News. Glassdoor and earnings transcripts are V2.

---

## Subagent Roster (V1)

| Agent | Sources | Output |
|---|---|---|
| company-bg | SEC, news, press releases | Snapshot, leadership, recent material events |
| tech-and-pain | crt.sh, GitHub, Glassdoor, news | Tech footprint, pain hypotheses with confidence |
| hiring-signals | Indeed, LinkedIn jobs | Hiring intensity, role specifics, intent signals |
| synthesizer | Reads all of above + persona.yml | Final brief JSON |

Each subagent file in `.claude/agents/` includes:
- System prompt with assigned sources
- Tool allowlist (only what it needs)
- Output schema (structured JSON, not free-form prose)
- Explicit failure mode: "If <source> returns nothing, output `{status: 'insufficient_data'}` — do not invent."

---

## Anti-Genericness Patterns (Synthesizer Prompt)

These prompt patterns fight the model's default toward safe/generic:

1. **Source attribution required.** Every claim outputs `[source_url, retrieved_date]`. Claims without sources get marked `[INFERENCE]` or dropped. This single constraint kills 60% of generic output.
2. **Negative few-shots in prompt.** Include one labeled GENERIC example brief and one labeled SPECIFIC example brief. Models follow examples harder than instructions.
3. **Specificity rewrite pass.** After draft, second LLM call: *"For each sentence, ask whether it could appear unchanged in a brief about any company in this industry. Rewrite or delete."*
4. **Confidence levels.** High / Medium / Inference badges on every claim.
5. **"Insufficient data" is valid.** Empty section beats filler. Prompt explicitly: *"If you cannot support a section with two sources, output 'insufficient data — would require: <specific source>'."*
6. **Trigger-driven discovery questions.** Synthesizer matches detected signals → trigger IDs in `persona.yml` → templates → fills with brief specifics. NOT free-form question generation.

---

## HTML Template Requirements

Single file. Tailwind CDN. No build step.

**Structure:**
- **Header band:** Company name, vertical badge, generation timestamp, favicon
- **TL;DR card:** 3 bullets, scannable in 30 seconds — the panel reads only this on first glance
- **Sectioned cards:**
  - Snapshot
  - Technical Footprint
  - Pain Hypotheses (confidence badges)
  - Hiring Signals
  - Discovery Questions (expandable, copy-to-clipboard per question)
  - Disqualifier Flags
- **Footnote chips** next to claims linking to sources
- **Footer:** agents involved, models used, runtime, "regenerate this section" hint

**Visual language:** Verkada-adjacent. Clean whites, structured cards, quiet color accents. Don't clone — same family. Subconsciously reads as *"this person already gets us."*

Print-friendly CSS for PDF export via browser print.

---

## Build Sequence (10 Days)

| Day | Focus |
|---|---|
| 1–2 | Persona YAML file (`verkada-se.yml`). Highest leverage. Iterate hardest. |
| 2–3 | Source-specific clients (SEC, Indeed, GitHub, crt.sh, Google News). Cache to JSON. |
| 3–5 | Subagents: company-bg, tech-and-pain, hiring-signals. System prompts + tool allowlists. |
| 5–6 | Synthesizer. Hardest prompt. Iterate with anti-genericness patterns. |
| 6–7 | HTML template + render step. Open-in-browser flow. |
| 7 | Pre-cache 2 demo accounts. One safe Verkada-fit (school district / hospital), one stretch (manufacturer / retailer). |
| 8–9 | Pitch narrative + dry run on a friend playing skeptic. Refine objection responses. |
| 10 | Buffer + final polish. |

---

## Demo Flow (10–12 min)

**1. Open with the problem, not the tool.**
> "Verkada SEs cover N named accounts. Best-case, the first 30–45 minutes per account is research before an intelligent first call. Across a quarter that's significant selling time. I built a tool that collapses that to ~90 seconds, with output that's better than what most SEs produce manually."

**2. Discovery moment (in-character).**
> "Before I show you — when your SEs prep an account today, what does that workflow actually look like? What tools? Where's the gap?"

Make the panel answer. This is your discovery moment in the role-play.

**3. Live demo.** Run `/research <pre-cached account>`. Walk through 3 specific things in the output a generic LLM wouldn't catch. Re-run discovery questions with two different persona inputs to show the persona file's leverage.

**4. Use case close.**
> "Rolled out across the SE org with a shared persona file owned by SE leadership: N hours/week back per SE. Adoption is a workflow decision, not a tooling decision."

**5. Q&A.**

---

## Anticipated Objections

| Objection | Response |
|---|---|
| *"ChatGPT does this."* | Run same prompt without persona file. Show generic output. Contrast. The persona is the moat. |
| *"Hallucination risk?"* | Every claim sourced inline with URL + retrieval date. Show in the HTML. |
| *"vs Apollo / Sales Nav / Clay?"* | Those are data layers. This is synthesis layer. Different problem. |
| *"How long did this take?"* | Honest answer. Velocity is an SE-positive trait. |
| *"Would you actually use this on the job?"* | Yes. Talk about how you'd evolve it with insider knowledge. |
| *"Why didn't our SEs build this?"* | Building tooling taxes quota time. SEs don't get rewarded for it. That's why bringing it pre-built is leverage. |

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Live demo fails (search returns garbage) | Pre-cache demo accounts. Disclose openly: *"I cached this run earlier."* |
| Generic output | Persona file rigor + anti-genericness prompt patterns |
| Tool overshadows candidate | Frame as *"workflow I'd run on day 1"*, not *"AI demo I built"* |
| Persona file insider gap | Frame as *"framework I'd refine with internal knowledge in week 1"* |
| Time runs out | V1 scope is brief + discovery prep ONLY. Other modules are roadmap. |

---

## V1 Scope (Locked)

**In:**
- `/research <company>` slash command
- 3 research subagents + synthesizer
- Persona-tuned discovery questions
- HTML output, browser-rendered
- 2 pre-cached demo accounts

**Out (Roadmap — mention in pitch):**
- `/displace <company>` competitive battle card
- `/demo-plan <company>` demo flow recommender
- `/size <company>` rough sizing
- `/followup <call_notes>` post-call email drafter
- `/refresh <company>` diff against prior brief
- MCP integrations (Crunchbase, BuiltWith)
- Notion / Salesforce export

---

## Open Decisions (Resolve at Build Time)

- Render step language: bash + Claude Code's file creation, or a small Python/Node script?
- Glassdoor scraping: V1 or V2? (Adds insider-pain signal but adds scraping fragility.)
- Discovery question count target per persona: 5 or 8? (More is not better.)
- TL;DR phrasing: bullets or short paragraph?

---

## Day 1 Actions

1. Create repo + project structure
2. Draft `verkada-se.yml` skeleton with the trigger taxonomy above. Iterate content.
3. Pick the two demo accounts. Lock them today — every prompt and persona detail tunes to making those briefs land.
4. Sketch the HTML template's visual hierarchy on paper before opening Tailwind. Brief structure first, then code.

---

## Final Note

The persona file is the unlock. Architecture is portable; the persona's rule taxonomy is what makes the output specific to Verkada and unforgettable to the panel. Spend disproportionate time there. Everything downstream amplifies whatever quality you put into that file.
