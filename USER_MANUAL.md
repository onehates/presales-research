# User Manual — OSINT for the SE

Multi-agent AI pre-sales research platform for Solutions Engineers. Takes a target account name, produces a sourced HTML discovery brief with MEDDIC qualification, champion projection, and persona-filtered discovery questions.

---

## 1. Setup

### Requirements

- Python 3.12+
- Linux/macOS (tested on Ubuntu 24.04)
- Internet access (for source client API calls)

### Environment Variables

| Variable | Required | Source | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | [console.anthropic.com](https://console.anthropic.com) | Claude API for subagents + synthesizer |
| `TAVILY_API_KEY` | Yes | [tavily.com](https://tavily.com) | News search, champion signal searches |
| `SAM_API_KEY` | Recommended | [sam.gov](https://sam.gov/content/entity-information) | SAM.gov entity lookup |
| `SERPAPI_KEY` | Optional | [serpapi.com](https://serpapi.com) | Indeed job posting scraping |

Add to your shell profile:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export TAVILY_API_KEY="tvly-..."
export SAM_API_KEY="..."
export SERPAPI_KEY="..."
```

> **Note:** Wrap values in double quotes. Bash treats `!` and other special characters as history expansion if unquoted.

### Dependencies

```bash
pip3 install --user anthropic requests pyyaml jinja2
```

Optional (for deliverable generation):
```bash
pip3 install --user python-pptx playwright
playwright install chromium
```

### Clone and verify

```bash
git clone <repo-url>
cd presales-research
python3 orchestrator.py --help
```

---

## 2. Running /research

### Basic usage

```bash
python3 orchestrator.py "Atlanta Public Schools"
```

### Flags

| Flag | Effect |
|---|---|
| `--force` | Ignore cache, re-fetch all sources |
| `--open` | Open the rendered HTML brief in default browser |

### What happens

The pipeline runs four phases:

**Phase 1 — Source Data Collection** (~30-90s)
18 source clients run in parallel (6 threads). Each client fetches data from a public API or website, writes structured JSON to `sources/{slug}/`, and reports status:
- Green checkmark: fresh data retrieved
- Cyan checkmark: cached data used (within TTL)
- Yellow dash: insufficient data (expected for some sources)
- Red X: error (API key missing, rate limited, etc.)

**Phase 1b — Deferred Clients** (~15-60s)
Champion signals client runs after Phase 1 (depends on leadership.json). Scores named individuals on 6 weighted factors via Tavily searches and Haiku topic extraction.

**Phase 2 — Subagent Synthesis** (~60-120s)
Three Sonnet subagents process source data sequentially (to avoid rate limits):
- `company-bg`: Company snapshot, leadership, federal funding, material events, vertical match
- `tech-and-pain`: Technical footprint, pain hypotheses, displacement intel, practitioner sentiment
- `hiring-signals`: Hiring intensity, security team signals, tech stack mentions

**Phase 3 — Synthesizer** (~60-90s)
Opus synthesizer merges all subagent outputs against `persona/verkada-se.yml`. Produces:
- TL;DR (3 bullets, 30-second skim)
- MEDDIC qualification with named champions
- Persona-filtered discovery questions from trigger templates
- Verkada GTM strategy
- Champion projection with score breakdowns

**Phase 4 — Render** (<1s)
JSON brief interpolated into HTML template. Output written to `briefs/{slug}-{date}.html`.

### Output locations

| File | Purpose |
|---|---|
| `briefs/{slug}-{date}.html` | Final rendered brief (open in browser) |
| `briefs/{slug}-{date}.json` | Structured brief data (read by other commands) |
| `sources/{slug}/*.json` | Raw source data cache |

### Timing

Typical end-to-end: **2-4 minutes** depending on API response times and cache state.

---

## 3. Understanding the Brief Output

### Section-by-section walkthrough

**TL;DR Card** (top of brief)
3 bullets max, priority-ordered. Each bullet is company-specific with sourced data. Confidence badge shows the lowest-confidence claim in the set.

**Company Snapshot**
Entity type, vertical, headquarters, size indicators, fiscal year. Propagated directly from company-bg subagent.

**Federal Funding Profile** (SLED accounts)
NCES enrollment, school count, FRPL percentage, Title I status, NDAA exposure assessment. Only appears for K-12 districts.

**Leadership**
Named individuals from news extraction + leadership page scraping. Role classification (Executive, IT, Security, Operations, Facilities, Board).

**Projected Champion Candidates**
Top 3 individuals scored on role fit, recency, career arc, public voice, topic affinity, authority. Per-factor score breakdown bars. Validation question per candidate. Disclaimer: "These are projections based on public signals. Validate via discovery."

**Recent Material Events**
Newsworthy events tagged by category and Verkada relevance. Source footnotes link to original articles.

**Vertical Match**
ICP vertical alignment from persona file. Key drivers present, typical pain overlap.

**Technical Footprint**
SSL certificate analysis, GitHub presence, tech stack mentions from job postings.

**Pain Hypotheses**
Inferred pain points with confidence levels and Verkada product mapping.

**Hiring Signals**
Job posting analysis: security roles, IT modernization signals, geographic expansion.

**Cooperative Purchasing**
Available vehicles (Sourcewell, OMNIA, HGACBuy, TIPS, COSTARS) with Verkada contract numbers, competitor manufacturers, discount tiers.

**Discovery Questions by Persona**
Questions grouped by buyer persona (IT Director, Director of Facilities, CSO, VP of Operations, Loss Prevention Director, Superintendent). Each question traces to a trigger ID and evidence string.

**MEDDIC Qualification**
7-field MEDDIC framework. Each field has: value, evidence array, confidence tag, gap analysis, source quality. Champion field uses named individuals when available.

**Verkada GTM Strategy**
Land play, POC strategy, channel partner, bundle recommendation, procurement path, expansion motion, competitive displacement. Grounded in specific subagent data.

**Disqualifier Flags**
Only flagged with affirmative evidence (e.g., active Verkada customer, on-prem-only mandate, recent competitor contract).

**Open Questions**
Gaps requiring discovery. Each names a specific missing source and priority level.

### Reading confidence badges

| Badge | Meaning |
|---|---|
| **High** (green) | Claim sourced from primary data (SEC filing, NCES record, official page) |
| **Medium** (yellow) | Claim sourced from secondary data (news article, job posting, Reddit) |
| **Inference** (red) | Claim inferred from indirect signals. Requires validation. |

### Reading source quality badges

| Badge | Meaning |
|---|---|
| **Primary** | Direct from authoritative source (government database, official filing) |
| **Secondary** | From credible third-party source (news outlet, industry publication) |
| **Weak** | From user-generated or unverified source (Reddit, forums, inferred) |

### Footnote chips

Every factual claim has a clickable footnote chip linking to the source URL. If a claim lacks a source, it's tagged `[INFERENCE]` with a red confidence badge.

---

## 4. Customizing the Persona File

### Structure

`persona/verkada-se.yml` is the rule engine that controls all synthesis behavior. It is **not** a description doc — it is configuration that the synthesizer executes against.

### Sections

**`product`** — Vendor name, product lines, positioning, key differentiators. Controls which product capabilities map to which pain points.

**`icp.verticals`** — Ideal customer profile by vertical. Each vertical has `key_drivers` and `typical_pain`. Controls vertical matching and pain hypothesis generation.

**`displacement_targets`** — Incumbent vendors with `common_pain` and `verkada_counter` text. Controls displacement intelligence section.

**`triggers`** — Detection signals mapped to discovery question templates. Each trigger has:
- `id`: Unique identifier referenced by synthesizer
- `detect_signals`: Keywords, job titles, NCES/Clery/HHS signals to look for
- `weight`: Priority score (0.0-1.0) controlling question surfacing order
- `discovery_templates`: Question templates with `{placeholder}` variables

**`leverage_references`** — Customer case studies mapped to trigger IDs. Appended inline to discovery questions as proof points.

**`personas`** — Buyer personas with `care_about` and `skip_topics` lists. Controls which discovery questions reach which persona. Each persona has a `meddic_role` (champion, economic_buyer, influencer).

**`champion_criteria`** — Weights, role priority, topic signals, vendor alumni indicators for champion projection scoring.

**`disqualifiers`** — Flags that should kill an opportunity (on-prem mandate, recent competitor contract, etc.).

### Adding a trigger

```yaml
triggers:
  - id: my_new_trigger
    detect_signals:
      keywords:
        - "specific keyword"
      source_hints:
        - news
        - job_postings
    weight: 0.8
    discovery_templates:
      - "I noticed {company} is {specific_signal}. How has that affected your security infrastructure decisions?"
```

### Making it vendor-agnostic

Copy `verkada-se.yml` to `cisco-se.yml` (or any vendor). Change:
- `product`: Your vendor's product lines and positioning
- `displacement_targets`: Your vendor's competitors
- `triggers`: Your vendor's relevant signals and question templates
- `champion_criteria.vendor_alumni_indicators`: Your vendor's customer/partner ecosystem
- `champion_criteria.champion_topic_signals`: Topics relevant to your solution

The source clients, subagents, and synthesizer are vendor-agnostic. Only the persona file changes.

---

## 5. Architecture Overview

### Source clients (`clients/`)

Each client is a Python module with a `fetch_*()` function that returns structured JSON. Clients handle their own caching, rate limiting, and error handling.

| Client | Source | Data |
|---|---|---|
| `sec.py` | SEC EDGAR | Filings, financials, company info |
| `nces.py` | NCES database | K-12 district demographics |
| `clery.py` | Clery Act reports | Campus crime statistics |
| `sam.py` | SAM.gov | Government entity registration |
| `hhs.py` | HHS OCR | HIPAA breach reports |
| `news.py` | Tavily Search | Recent news articles |
| `indeed.py` | Indeed/SerpAPI | Job postings |
| `crtsh.py` | crt.sh | SSL certificates (infrastructure) |
| `github.py` | GitHub API | Organization repositories |
| `reddit.py` | Reddit search | Practitioner discussions |
| `leadership.py` | News + web scrape | Named individuals + titles |
| `champion_signals.py` | Tavily + Haiku | Per-individual signal scoring |
| `sourcewell.py` | Sourcewell | Cooperative contracts |
| `tips.py` | TIPS-USA | Cooperative contracts |
| `omnia.py` | OMNIA Partners | Cooperative contracts |
| `hgac.py` | HGACBuy | Cooperative contracts + discounts |
| `costars.py` | COSTARS | PA cooperative contracts |
| `ga_procurement.py` | Georgia state | State procurement |
| `atlanta_procurement.py` | City of Atlanta | City procurement |
| `sled_procurement.py` | SLED-specific | Education procurement |

### Model assignment

| Task | Model | Why |
|---|---|---|
| Source parsing, extraction | Haiku | Fast, cheap, structured output |
| Subagent reasoning | Sonnet | Per-source synthesis, good enough |
| Final synthesizer | Opus | Cross-source synthesis, anti-genericness |
| Specificity rewrite | Opus | Highest quality for final pass |
| Champion topic extraction | Haiku | Small task, structured output |

### Cache strategy

| Source | TTL |
|---|---|
| SEC filings | 90 days |
| News | 7 days |
| Job postings | 14 days |
| GitHub | 30 days |
| Leadership | 30 days |
| Champion signals | 30 days |
| Cooperative purchasing | Shared in `sources/_market/` |

---

## 6. Troubleshooting

### "ANTHROPIC_API_KEY not set"

The orchestrator checks for this env var before running subagents. Verify:
```bash
echo $ANTHROPIC_API_KEY
```
If empty, add to your shell profile and re-source it.

### Rate limiting (429 errors)

The orchestrator has built-in retry with exponential backoff (5s, 15s, 45s). Phase 2 subagents run sequentially (not parallel) to minimize rate limit hits. If you still hit limits, wait 60 seconds and re-run — the cache will skip completed sources.

### "insufficient_data" everywhere

This is a valid output. Some sources won't have data for every company:
- NCES only covers K-12 districts
- Clery only covers higher ed institutions
- HHS only covers HIPAA-covered entities
- SEC only covers publicly traded companies

The synthesizer handles missing subagent data gracefully. The brief can be generated with 2 of 3 subagents (but not without company-bg).

### Subagent parse errors

If a subagent returns unparseable output, the orchestrator attempts JSON extraction from the response. If that fails, the subagent output is saved as `parse_error` with the raw text in `sources/{slug}/`. Check the raw output to diagnose prompt issues.

### Leadership extraction is noisy

The leadership client uses two extraction strategies: Haiku NLP over news articles + HTML scraping of leadership pages. Some noise is expected (concatenated names, titles extracted as names). The client has cleanup filters but edge cases exist. The champion signals client downstream handles this by requiring name + title for scoring.

### Brief renders but looks wrong

The HTML template uses Tailwind CDN. If you see unstyled HTML, check your internet connection (Tailwind loads from CDN). For offline use, download Tailwind and update the `<script>` tag in `templates/brief.html`.

---

## 7. API Tier Comparison

| | V1 (Built) | V2 (Planned) | V3 (Enterprise) |
|---|---|---|---|
| **Data sources** | 18 free public APIs | + Apollo, per-district RFPs | + CRM, internal data |
| **Contact data** | Names + titles from OSINT | Direct dials, org charts | CRM-synced contacts |
| **Champion scoring** | Public signals only | + LinkedIn profile data | + CRM interaction history |
| **Cost** | API keys only (~$5-20/mo) | + Apollo subscription | Enterprise licensing |
| **Setup time** | 10 minutes | 30 minutes | Integration project |
| **Best for** | Individual SE prep | SE team deployment | Org-wide platform |

---

## 8. Known Limitations

- **No LinkedIn scraping.** Champion signals use Tavily search for public career data. LinkedIn profile data would significantly improve career arc and tenure scoring but requires API access (V2).
- **Cooperative purchasing is Southeast-biased.** GA procurement, Atlanta procurement, and SLED procurement clients are Georgia-specific. Other states need state-specific clients.
- **Free-tier Tavily limits.** ~1,000 searches/month on free tier. At ~15 searches per company (news + champion signals), that's ~66 companies/month. Paid tier removes this limit.
- **Leadership page scraping is brittle.** Different organizations structure their leadership pages differently. The client handles common patterns (heading=name, heading=title) but will miss unconventional layouts.
- **Haiku topic extraction is approximate.** The champion signals topic affinity score depends on Haiku correctly extracting topics from short text snippets. Results are inference-quality.
- **No real-time updates.** The tool runs on-demand. There's no monitoring or alerting for changes in source data between runs.
