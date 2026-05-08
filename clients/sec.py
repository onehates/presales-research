"""
SEC EDGAR Client — Fetches 10-K and 10-Q filings, extracts risk factors and material events.

Language choice: Python
  - Aligns with render/render.py (project layout uses Python for data processing)
  - All dependencies already installed: requests, beautifulsoup4, anthropic
  - stdlib json/re/time sufficient for EDGAR's REST API

EDGAR API surfaces used:
  1. company_tickers.json  — CIK resolution from company name/ticker
  2. data.sec.gov/submissions — Filing metadata (accession numbers, dates, form types)
  3. www.sec.gov/Archives   — Full filing document text

Rate limit: 10 req/sec enforced via time.sleep(0.1) between requests.
Extraction: Claude Haiku per CLAUDE.md model assignment.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "sources"

EDGAR_UA = "PresalesResearch research@presales-tool.dev"
EDGAR_HEADERS = {
    "User-Agent": EDGAR_UA,
    "Accept-Encoding": "gzip, deflate",
}

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_raw}/{accession_no_dashes}/{primary_doc}"

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# How many of each filing type to fetch
MAX_10K = 1
MAX_10Q = 2

# Cache TTL for SEC filings: 90 days (per CLAUDE.md)
CACHE_TTL_DAYS = 90

# Rate-limit: minimum seconds between EDGAR requests
REQUEST_INTERVAL = 0.12  # ~8 req/sec, safely under 10

_last_request_time = 0.0


# ---------------------------------------------------------------------------
# Rate-limited HTTP
# ---------------------------------------------------------------------------

def _edgar_get(url: str, *, timeout: int = 30) -> requests.Response:
    """GET with User-Agent header and rate limiting."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)

    resp = requests.get(url, headers=EDGAR_HEADERS, timeout=timeout)
    _last_request_time = time.time()

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 10))
        print(f"  [rate-limited] sleeping {retry_after}s…", file=sys.stderr)
        time.sleep(retry_after)
        return _edgar_get(url, timeout=timeout)

    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Step 1: Resolve company name → CIK
# ---------------------------------------------------------------------------

def resolve_cik(company_name: str) -> dict:
    """
    Search company_tickers.json for a match by name or ticker.
    Returns {"cik": "0000320193", "cik_raw": 320193, "ticker": "AAPL", "name": "Apple Inc."} or raises.
    """
    print(f"  [cik] resolving '{company_name}'…", file=sys.stderr)
    resp = _edgar_get(TICKERS_URL)
    tickers = resp.json()

    query = company_name.strip().lower()

    # Normalize common suffixes for fuzzy matching
    # "Target Corporation" should match "TARGET CORP", "Apple Inc." should match "APPLE INC"
    def _normalize(s: str) -> str:
        s = s.lower().strip()
        for long, short in [("corporation", "corp"), ("incorporated", "inc"), ("company", "co"), ("limited", "ltd")]:
            s = s.replace(long, short)
        # Strip trailing punctuation and common suffixes for comparison
        s = re.sub(r"[.,]+$", "", s).strip()
        return s

    query_norm = _normalize(query)

    # Pass 1: exact ticker match
    for entry in tickers.values():
        if entry["ticker"].lower() == query:
            return _format_cik_result(entry)

    # Pass 2: exact normalized name match
    for entry in tickers.values():
        if _normalize(entry["title"]) == query_norm:
            return _format_cik_result(entry)

    # Pass 3: title starts with query or query starts with title (word-boundary)
    # "Target" matches "TARGET CORP" but not "TECHTARGET"
    matches = []
    for entry in tickers.values():
        title_norm = _normalize(entry["title"])
        if title_norm.startswith(query_norm) or query_norm.startswith(title_norm):
            matches.append(entry)

    # Pass 4: token overlap — all query words appear in title
    if not matches:
        query_tokens = set(query_norm.split())
        for entry in tickers.values():
            title_tokens = set(_normalize(entry["title"]).split())
            if query_tokens and query_tokens.issubset(title_tokens):
                matches.append(entry)

    # Pass 5: substring as fallback, but only if query is 5+ chars to avoid noise
    if not matches and len(query_norm) >= 5:
        for entry in tickers.values():
            title_norm = _normalize(entry["title"])
            if query_norm in title_norm:
                matches.append(entry)

    if not matches:
        raise CompanyNotFoundError(
            f"No SEC-registered company found matching '{company_name}'. "
            "Try using the stock ticker symbol or exact legal name."
        )

    # Prefer shortest name (most specific match), then lowest CIK
    best = min(matches, key=lambda e: (len(e["title"]), e["cik_str"]))
    if len(matches) > 1:
        print(
            f"  [cik] {len(matches)} matches; selecting '{best['title']}' (CIK {best['cik_str']})",
            file=sys.stderr,
        )
    return _format_cik_result(best)


def _format_cik_result(entry: dict) -> dict:
    cik_int = entry["cik_str"]
    return {
        "cik": str(cik_int).zfill(10),
        "cik_raw": cik_int,
        "ticker": entry["ticker"],
        "name": entry["title"],
    }


# ---------------------------------------------------------------------------
# Step 2: Fetch filing metadata
# ---------------------------------------------------------------------------

def fetch_filing_metadata(cik: str) -> dict:
    """Fetch submissions JSON for a CIK. Returns the full submissions object."""
    url = SUBMISSIONS_URL.format(cik=cik)
    print(f"  [filings] fetching submissions for CIK {cik}…", file=sys.stderr)
    resp = _edgar_get(url)
    return resp.json()


def select_filings(submissions: dict, form_type: str, max_count: int) -> list[dict]:
    """
    Pick the most recent filings of a given form type from submissions.recent.
    Returns list of {"accession": ..., "filing_date": ..., "primary_doc": ..., "report_date": ...}.
    """
    recent = submissions["filings"]["recent"]
    forms = recent["form"]
    results = []

    for i, form in enumerate(forms):
        if form == form_type and len(results) < max_count:
            results.append({
                "accession": recent["accessionNumber"][i],
                "filing_date": recent["filingDate"][i],
                "report_date": recent.get("reportDate", [""])[i] if i < len(recent.get("reportDate", [])) else "",
                "primary_doc": recent["primaryDocument"][i],
                "primary_doc_description": recent.get("primaryDocDescription", [""])[i] if i < len(recent.get("primaryDocDescription", [])) else "",
            })

    return results


# ---------------------------------------------------------------------------
# Step 3: Fetch and parse filing HTML
# ---------------------------------------------------------------------------

def fetch_filing_text(cik_raw: int, filing: dict) -> str:
    """Download filing HTML and extract readable text."""
    accession_no_dashes = filing["accession"].replace("-", "")
    url = FILING_URL.format(
        cik_raw=cik_raw,
        accession_no_dashes=accession_no_dashes,
        primary_doc=filing["primary_doc"],
    )
    print(f"  [fetch] {filing['primary_doc']} ({filing['filing_date']})…", file=sys.stderr)
    resp = _edgar_get(url, timeout=60)

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove script/style noise
    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)

    # Collapse excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text


def extract_section(text: str, section_pattern: str, next_section_pattern: str) -> str:
    """
    Extract text between two section header patterns.
    Returns the matched section or empty string.
    """
    match = re.search(section_pattern, text, re.IGNORECASE)
    if not match:
        return ""

    start = match.start()
    end_match = re.search(next_section_pattern, text[match.end():], re.IGNORECASE)
    if end_match:
        end = match.end() + end_match.start()
    else:
        # Take up to 50k chars if no end marker found
        end = min(start + 50000, len(text))

    section = text[start:end].strip()
    # Truncate to ~40k chars to stay within Haiku context limits
    if len(section) > 40000:
        section = section[:40000] + "\n[…truncated]"
    return section


def extract_risk_factors_section(text: str) -> str:
    """Pull Item 1A (Risk Factors) from 10-K/10-Q text.

    Finds all 'Item 1A...Risk Factors' matches and picks the one followed by
    the most content (skips TOC entries which are just short references).
    """
    pattern = re.compile(r"item\s+1a[\.\s\-—–]*risk\s+factors", re.IGNORECASE)
    end_pattern = r"item\s+1b[\.\s\-—–]|item\s+2[\.\s\-—–]"

    best = ""
    for match in pattern.finditer(text):
        start = match.start()
        end_match = re.search(end_pattern, text[match.end():], re.IGNORECASE)
        if end_match:
            end = match.end() + end_match.start()
        else:
            end = min(start + 50000, len(text))
        candidate = text[start:end].strip()
        # Pick the longest candidate (real section, not TOC reference)
        if len(candidate) > len(best):
            best = candidate

    if len(best) > 40000:
        best = best[:40000] + "\n[…truncated]"
    return best


def extract_material_events_section(text: str) -> str:
    """Pull Item 8.01 / Item 5 / forward-looking sections from text."""
    # Try multiple patterns for material event sections
    for pattern, next_pat in [
        (r"item\s+8[\.\s]*01", r"item\s+9[\.\s]|signatures"),
        (r"item\s+5[\.\s\-—–]*other\s+events", r"item\s+[6-9][\.\s]|signatures"),
        (r"forward[\-\s]looking\s+statements", r"item\s+\d|signatures"),
    ]:
        section = extract_section(text, pattern, next_pat)
        if section and len(section) > 100:
            return section
    return ""


# ---------------------------------------------------------------------------
# Step 4: LLM extraction via Haiku
# ---------------------------------------------------------------------------

def _load_persona_context() -> str:
    """Load Verkada product lines and differentiators from persona file for Haiku context."""
    persona_path = PROJECT_ROOT / "persona" / "verkada-se.yml"
    if not persona_path.exists():
        return ""
    try:
        import yaml
        persona = yaml.safe_load(persona_path.read_text())
    except ImportError:
        # Fallback: extract key lines without PyYAML
        raw = persona_path.read_text()
        lines = []
        capture = False
        for line in raw.split("\n"):
            if line.strip().startswith("product:") or line.strip().startswith("lines:") or line.strip().startswith("key_differentiators:") or line.strip().startswith("displacement_targets:"):
                capture = True
            if capture:
                lines.append(line)
            if capture and line.strip() == "" and len(lines) > 3:
                capture = False
        return "\n".join(lines)
    except Exception:
        return ""

    product = persona.get("product", {})
    displace = persona.get("displacement_targets", [])

    lines = [f"Verkada product lines: {', '.join(product.get('lines', []))}"]
    lines.append(f"Unified under: {product.get('unified_under', 'Command platform')}")
    lines.append(f"Positioning: {product.get('positioning', '')}")

    diffs = product.get("key_differentiators", [])
    if diffs:
        lines.append("Key differentiators:")
        for d in diffs:
            if isinstance(d, dict):
                for k, v in d.items():
                    lines.append(f"  - {k}: {v}")
            else:
                lines.append(f"  - {d}")

    if displace:
        vendors = [d.get("vendor", "") for d in displace if isinstance(d, dict)]
        lines.append(f"Displacement targets (incumbent vendors): {', '.join(vendors)}")

    return "\n".join(lines)


def extract_with_haiku(text: str, extraction_type: str, company_name: str, filing_date: str, source_url: str = "") -> dict:
    """
    Use Claude Haiku to extract structured data from filing text.
    extraction_type: "risk_factors" or "material_events"
    """
    if not text or len(text.strip()) < 50:
        return {"status": "insufficient_data", "reason": "Section text too short or missing from filing"}

    client = anthropic.Anthropic()
    persona_ctx = _load_persona_context()

    if extraction_type == "risk_factors":
        system_prompt = (
            "You are a financial document parser extracting risk factors from SEC filings "
            "for a Verkada Solutions Engineer's pre-sales research tool.\n\n"
            "## Verkada Context (use this to judge relevance)\n"
            f"{persona_ctx}\n\n"
            "## Output Schema\n"
            "Output ONLY valid JSON with no markdown formatting. Use this exact schema:\n"
            '{"risks": [{"title": "short label", '
            '"summary": "2-3 sentence summary of the SPECIFIC risk — must reference company-specific details (names, dollar amounts, geographies, systems)", '
            '"category": "one of: operational|financial|regulatory|competitive|cybersecurity|legal|market|supply_chain|environmental", '
            '"confidence": "one of: high|medium|inference — high = explicitly stated in filing language, medium = clearly implied by context, inference = your interpretation of vague language", '
            '"source_section": "the Item 1A sub-heading or paragraph topic this risk was extracted from", '
            '"verkada_relevant": true/false}]}\n\n'
            "## verkada_relevant Criteria\n"
            "Flag true ONLY if the risk directly relates to one of these Verkada-addressable domains:\n"
            "- Physical security (cameras, surveillance, monitoring)\n"
            "- Access control (badge, door, visitor management)\n"
            "- Workplace safety, environmental sensors, alarms\n"
            "- IT infrastructure burden from on-prem security systems (NVR/DVR/VMS servers)\n"
            "- NDAA compliance, supply chain security for hardware\n"
            "- Facility expansion, new construction, multi-site management\n"
            "- Any mention of incumbent vendors: Avigilon, Genetec, Milestone, Lenel, Hikvision, Dahua\n"
            "Do NOT flag generic IT/cyber risks as verkada_relevant unless they specifically mention physical security.\n\n"
            "## Anti-Genericness Rules (MANDATORY)\n"
            "- Every summary must be specific to THIS company. If a sentence could appear unchanged in a "
            "different company's brief, rewrite it with company-specific details or drop it.\n"
            "- If a risk factor is pure legal boilerplate with no company-specific substance, skip it entirely.\n"
            "- Do NOT use hedging words (likely, potentially, may) unless paired with confidence: inference.\n"
            "- If fewer than 3 non-boilerplate risks are extractable, return:\n"
            '  {"risks": [], "status": "insufficient_data", "reason": "Filing risk factors are predominantly boilerplate"}\n'
            "- Extract the TOP 15 most material risks, not all of them.\n"
        )
    else:
        system_prompt = (
            "You are a financial document parser extracting material events from SEC filings "
            "for a Verkada Solutions Engineer's pre-sales research tool.\n\n"
            "## Verkada Context (use this to judge relevance)\n"
            f"{persona_ctx}\n\n"
            "## Output Schema\n"
            "Output ONLY valid JSON with no markdown formatting. Use this exact schema:\n"
            '{"events": [{"description": "what happened or is planned — MUST include specific details: dollar amounts, names, locations, dates", '
            '"date": "YYYY-MM-DD if mentioned, otherwise null", '
            '"category": "one of: acquisition|divestiture|leadership|restructuring|expansion|litigation|regulatory|capital_project|other", '
            '"confidence": "one of: high|medium|inference — high = explicitly stated with date/amount, medium = stated but vague on details, inference = implied from context", '
            '"source_section": "the sub-heading or paragraph topic this event was extracted from", '
            '"verkada_relevant": true/false}]}\n\n'
            "## verkada_relevant Criteria\n"
            "Flag true ONLY if the event directly relates to:\n"
            "- New facilities, buildings, campuses, renovations (capital projects = greenfield security)\n"
            "- Safety incidents, workplace violence, theft, security breaches\n"
            "- Physical security system changes, vendor switches, infrastructure upgrades\n"
            "- Regulatory actions related to physical security, NDAA, facility compliance\n"
            "- Acquisitions that add new sites requiring security integration\n"
            "Do NOT flag generic M&A or financial events as verkada_relevant unless they involve facilities.\n\n"
            "## Anti-Genericness Rules (MANDATORY)\n"
            "- Every description must include company-specific details. No generic event descriptions.\n"
            "- Do NOT use hedging words (likely, potentially, may) unless paired with confidence: inference.\n"
            "- If no material events are extractable with specifics, return:\n"
            '  {"events": [], "status": "insufficient_data", "reason": "describe what is missing"}\n'
            "- Extract up to 10 most material events.\n"
        )

    user_msg = (
        f"Company: {company_name}\n"
        f"Filing date: {filing_date}\n"
        f"Source URL: {source_url}\n"
        f"Section text:\n\n{text}"
    )

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if Haiku wraps them
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        return json.loads(raw)
    except json.JSONDecodeError as e:
        return {"status": "extraction_error", "reason": f"Haiku returned invalid JSON: {e}", "raw_snippet": raw[:500]}
    except anthropic.APIError as e:
        return {"status": "extraction_error", "reason": f"Anthropic API error: {e}"}
    except TypeError as e:
        # No API key configured — expected when running outside Claude Code runtime
        if "api_key" in str(e) or "auth_token" in str(e):
            return {"status": "extraction_error", "reason": "ANTHROPIC_API_KEY not set. Set the env var or run via Claude Code."}
        raise


# ---------------------------------------------------------------------------
# Step 5: Cache management
# ---------------------------------------------------------------------------

def cache_path(company_slug: str) -> Path:
    return SOURCES_DIR / company_slug / "sec.json"


def read_cache(company_slug: str) -> dict | None:
    """Read cached sec.json if it exists and is within TTL."""
    path = cache_path(company_slug)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    retrieved_at = data.get("retrieved_at")
    if not retrieved_at:
        return None

    retrieved_dt = datetime.fromisoformat(retrieved_at)
    age_days = (datetime.now(timezone.utc) - retrieved_dt).days
    if age_days > CACHE_TTL_DAYS:
        print(f"  [cache] sec.json is {age_days}d old (TTL={CACHE_TTL_DAYS}d), refetching", file=sys.stderr)
        return None

    print(f"  [cache] sec.json is {age_days}d old, within TTL", file=sys.stderr)
    return data


def write_cache(company_slug: str, data: dict) -> Path:
    """Write structured data to sources/{company}/sec.json."""
    path = cache_path(company_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))
    print(f"  [cache] wrote {path}", file=sys.stderr)
    return path


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CompanyNotFoundError(Exception):
    pass


class NoRecentFilingsError(Exception):
    pass


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Convert company name to filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


def fetch_sec_data(company_name: str, *, force_refresh: bool = False) -> dict:
    """
    Full pipeline: resolve CIK → fetch filings → extract risk factors & events → cache.

    Returns the structured JSON dict (also written to sources/{company}/sec.json).
    """
    company_slug = slugify(company_name)

    # Check cache first
    if not force_refresh:
        cached = read_cache(company_slug)
        if cached is not None:
            return cached

    # Step 1: Resolve CIK
    cik_info = resolve_cik(company_name)
    print(f"  [cik] resolved → {cik_info['name']} ({cik_info['ticker']}, CIK {cik_info['cik']})", file=sys.stderr)

    # Step 2: Fetch filing metadata
    submissions = fetch_filing_metadata(cik_info["cik"])

    filings_10k = select_filings(submissions, "10-K", MAX_10K)
    filings_10q = select_filings(submissions, "10-Q", MAX_10Q)

    if not filings_10k and not filings_10q:
        raise NoRecentFilingsError(
            f"No 10-K or 10-Q filings found for {cik_info['name']} (CIK {cik_info['cik']}). "
            "This company may be a non-reporting entity or foreign private issuer."
        )

    print(f"  [filings] found {len(filings_10k)} 10-K, {len(filings_10q)} 10-Q", file=sys.stderr)

    # Step 3 & 4: Fetch text and extract via Haiku
    filing_results = []

    for filing in filings_10k + filings_10q:
        form_type = "10-K" if filing in filings_10k else "10-Q"
        text = fetch_filing_text(cik_info["cik_raw"], filing)

        risk_text = extract_risk_factors_section(text)
        events_text = extract_material_events_section(text)

        print(
            f"  [extract] {form_type} {filing['filing_date']}: "
            f"risk_factors={len(risk_text)} chars, events={len(events_text)} chars",
            file=sys.stderr,
        )

        accession_no_dashes = filing["accession"].replace("-", "")
        source_url = FILING_URL.format(
            cik_raw=cik_info["cik_raw"],
            accession_no_dashes=accession_no_dashes,
            primary_doc=filing["primary_doc"],
        )

        risk_data = extract_with_haiku(risk_text, "risk_factors", cik_info["name"], filing["filing_date"], source_url)
        events_data = extract_with_haiku(events_text, "material_events", cik_info["name"], filing["filing_date"], source_url)

        filing_results.append({
            "form_type": form_type,
            "filing_date": filing["filing_date"],
            "report_date": filing["report_date"],
            "accession_number": filing["accession"],
            "source_url": source_url,
            "risk_factors": risk_data,
            "material_events": events_data,
        })

    # Step 5: Assemble and cache
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "sec_edgar",
        "company": {
            "name": cik_info["name"],
            "ticker": cik_info["ticker"],
            "cik": cik_info["cik"],
            "sic": submissions.get("sic", ""),
            "sic_description": submissions.get("sicDescription", ""),
            "state_of_incorporation": submissions.get("stateOfIncorporation", ""),
            "fiscal_year_end": submissions.get("fiscalYearEnd", ""),
            "category": submissions.get("category", ""),
        },
        "filings": filing_results,
        "summary": {
            "total_filings_analyzed": len(filing_results),
            "verkada_relevant_risks": _count_relevant(filing_results, "risk_factors", "risks"),
            "verkada_relevant_events": _count_relevant(filing_results, "material_events", "events"),
        },
    }

    write_cache(company_slug, result)
    return result


def _count_relevant(filing_results: list, section_key: str, items_key: str) -> int:
    """Count items flagged verkada_relevant across all filings."""
    count = 0
    for f in filing_results:
        items = f.get(section_key, {}).get(items_key, [])
        count += sum(1 for item in items if item.get("verkada_relevant"))
    return count


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python sec_client.py <company_name> [--force]", file=sys.stderr)
        print("  e.g.: python sec_client.py 'Target Corporation'", file=sys.stderr)
        print("        python sec_client.py AAPL --force", file=sys.stderr)
        sys.exit(1)

    company_name = sys.argv[1]
    force = "--force" in sys.argv

    try:
        result = fetch_sec_data(company_name, force_refresh=force)
        # Print summary to stderr, full JSON to stdout (for piping)
        summary = result["summary"]
        print(
            f"\n  Done: {result['company']['name']} ({result['company']['ticker']})\n"
            f"  Filings analyzed: {summary['total_filings_analyzed']}\n"
            f"  Verkada-relevant risks: {summary['verkada_relevant_risks']}\n"
            f"  Verkada-relevant events: {summary['verkada_relevant_events']}\n"
            f"  Cached to: sources/{slugify(company_name)}/sec.json",
            file=sys.stderr,
        )
        print(json.dumps(result, indent=2, default=str))

    except CompanyNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except NoRecentFilingsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(3)
    except requests.HTTPError as e:
        print(f"ERROR: SEC EDGAR HTTP error: {e}", file=sys.stderr)
        sys.exit(4)


if __name__ == "__main__":
    main()
