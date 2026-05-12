"""
Leadership Client — Named individual discovery for champion identification.

Two extraction layers:
  1. News extraction: Haiku pass over cached news.json to extract named individuals
     with titles, quotes, and dates from existing news articles.
  2. Leadership page scrape: Try common URL patterns for the entity's public
     leadership/administration page. Parse named people with titles.

Data source: Cached news.json + entity website leadership pages
  - News extraction: Uses Claude Haiku (per CLAUDE.md model assignment)
  - Web scrape: requests + BeautifulSoup (no JS rendering)
  - LinkedIn search URLs: Generated templates, not scraped

Fragility points:
  1. Leadership pages vary wildly in structure. Many are behind JS frameworks
     (React/Angular) that requests can't render. Success rate ~40-60%.
  2. News extraction depends on news.json quality — if news articles don't mention
     the target entity's leaders by name, extraction yields nothing.
  3. K-12 districts often have leadership pages at non-standard paths
     (/superintendent-s-office, /district-leadership-team, etc.).
  4. LinkedIn URLs are search templates only — not validated against real profiles.

Cache TTL: 30 days (leadership changes slowly).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import requests

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "sources"

HAIKU_MODEL = "claude-haiku-4-5-20251001"
CACHE_TTL_DAYS = 30
REQUEST_TIMEOUT = 15

# Common leadership page paths by entity type
LEADERSHIP_PATHS_GENERAL = [
    "/about/leadership",
    "/leadership",
    "/about/executive-leadership",
    "/executive-leadership",
    "/about/our-team",
    "/our-team",
    "/about",
    "/administration",
    "/staff",
]

LEADERSHIP_PATHS_K12 = [
    "/administration",
    "/district-leadership",
    "/superintendent",
    "/board-of-education",
    "/board",
    "/about/leadership",
    "/leadership",
    "/district-leadership-team",
    "/about/administration",
    "/superintendent-s-office",
]

LEADERSHIP_PATHS_HIGHER_ED = [
    "/administration",
    "/president",
    "/provost",
    "/cabinet",
    "/about/leadership",
    "/leadership",
    "/president/cabinet",
    "/about/administration",
    "/executive-leadership",
]

# Headers to look like a browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _cache_path(company_name: str) -> Path:
    slug = _slugify(company_name)
    return SOURCES_DIR / slug / "leadership.json"


def _cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        retrieved = data.get("retrieved_at", "")
        if not retrieved:
            return False
        from datetime import timedelta
        ts = datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - ts
        return age < timedelta(days=CACHE_TTL_DAYS)
    except (json.JSONDecodeError, ValueError, KeyError):
        return False


# ---------------------------------------------------------------------------
# Layer 1a: News-based name extraction (Haiku)
# ---------------------------------------------------------------------------

def _extract_names_from_news(company_name: str, slug: str) -> list[dict]:
    """Run Haiku over cached news.json to extract named individuals."""
    news_path = SOURCES_DIR / slug / "news.json"
    if not news_path.exists():
        return []

    try:
        news_data = json.loads(news_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    articles = news_data.get("articles", [])
    if not articles:
        return []

    # Filter to articles that are actually about the target company
    # (news.json sometimes has tangential results)
    relevant_articles = []
    company_lower = company_name.lower()
    company_words = set(company_lower.split())
    for art in articles:
        title = (art.get("title") or "").lower()
        content = (art.get("content") or "").lower()
        # Check if company name or significant words appear
        if company_lower in title or company_lower in content:
            relevant_articles.append(art)
        elif len(company_words) >= 2 and sum(1 for w in company_words if w in title or w in content) >= 2:
            relevant_articles.append(art)

    if not relevant_articles:
        return []

    # Build article text for Haiku
    article_text = []
    for art in relevant_articles[:15]:  # Cap at 15 articles
        entry = f"Title: {art.get('title', '')}\nURL: {art.get('url', '')}\nDate: {art.get('published_date', 'unknown')}\nContent: {(art.get('content') or '')[:1500]}"
        article_text.append(entry)

    prompt_text = "\n---\n".join(article_text)

    if not anthropic or not os.environ.get("ANTHROPIC_API_KEY"):
        return []

    system = (
        "You extract named individuals from news articles. "
        "Output ONLY a JSON array of objects. No markdown, no explanation.\n"
        "Each object: {\"name\": \"Full Name\", \"title\": \"Their Title/Role\", "
        "\"quote_excerpt\": \"Direct quote if any, else null\", "
        "\"date\": \"YYYY-MM-DD or null\", \"source_url\": \"article URL\", "
        "\"context\": \"One sentence about what they did/said\"}\n"
        "RULES:\n"
        f"- Only extract people who work at or lead {company_name} (not other companies)\n"
        "- Include: executives, board members, superintendents, directors, principals, deans\n"
        "- Exclude: reporters, analysts, politicians (unless they serve on the entity's board)\n"
        "- If no relevant named individuals found, output []\n"
        "- Be precise with names — use the exact name as it appears in the article"
    )

    user_msg = (
        f"Extract named individuals associated with {company_name} from these articles:\n\n"
        f"{prompt_text}"
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
        # Strip markdown fences
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)
        return json.loads(text)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Layer 1b: Leadership page scrape
# ---------------------------------------------------------------------------

def _guess_domain(company_name: str, slug: str) -> list[str]:
    """Guess likely website domains for the entity.

    Prioritizes domains that contain words from the company name (likely
    the entity's own website) over generic news/blog domains.
    """
    # Collect candidate domains with priority scoring
    candidates = {}  # domain -> priority (lower = better)
    company_lower = company_name.lower()
    company_words = set(re.sub(r'[^a-z0-9\s]', '', company_lower).split())
    # Remove common filler words
    company_words -= {"the", "of", "and", "for", "in", "at", "a", "an"}

    # Skip list: news, social, job, and media domains
    skip_patterns = {
        "youtube.com", "msn.com", "fox5atlanta.com", "wsbtv.com",
        "ajc.com", "patch.com", "twitter.com", "instagram.com",
        "facebook.com", "linkedin.com", "reddit.com", "ziprecruiter.com",
        "indeed.com", "securitymagazine.com", "govtech.com",
        "thehackernews.com", "techcrunch.com", "bloomberg.com",
        "reuters.com", "apnews.com", "cnn.com", "nytimes.com",
        "washingtonpost.com", "bbc.com", "theguardian.com",
        "yahoo.com", "google.com", "11alive.com", "wjcl.com",
        "securityweek.com", "atlantanewsfirst.com", "gpb.org",
        "bizwest.com", "constructionbroadsheet.com",
    }

    # Significant words for domain matching (exclude city/state names which appear in news domains)
    city_state_words = {"atlanta", "georgia", "new", "york", "los", "angeles", "chicago",
                        "houston", "dallas", "san", "francisco", "boston", "seattle",
                        "denver", "phoenix", "philadelphia", "portland", "miami", "tampa"}
    significant_words = company_words - city_state_words

    # Build abbreviation variants for matching (e.g., "technology" -> "tech")
    abbrev_map = {
        "technology": "tech", "institute": "inst", "university": "univ",
        "school": "sch", "schools": "schl", "public": "pub",
        "district": "dist", "national": "natl", "international": "intl",
    }
    abbreviations = set()
    for w in significant_words:
        if w in abbrev_map:
            abbreviations.add(abbrev_map[w])

    def _score_domain(host: str) -> int:
        """Score a domain. Lower = more likely the entity's own site."""
        host_lower = host.lower().replace("www.", "")
        # Check if significant company name words appear in domain
        sig_hits = sum(1 for w in significant_words if w in host_lower and len(w) > 3)
        # Also check abbreviations (e.g., "tech" for "technology")
        abbrev_hits = sum(1 for a in abbreviations if a in host_lower)
        all_hits = sum(1 for w in company_words if w in host_lower and len(w) > 2)
        combined_sig = sig_hits + abbrev_hits
        if combined_sig >= 2:
            return 0  # High confidence: multiple significant/abbreviated company words
        if combined_sig == 1 and all_hits >= 1:
            return 1  # Good: one significant + another word match
        if combined_sig == 1:
            return 3  # Medium: one significant word
        if all_hits >= 1:
            return 7  # Low: only city/state words match
        return 10  # No match

    # Check news.json for entity-owned domains
    news_path = SOURCES_DIR / slug / "news.json"
    if news_path.exists():
        try:
            news_data = json.loads(news_path.read_text())
            for art in news_data.get("articles", []):
                url = art.get("url", "")
                if url:
                    parsed = urlparse(url)
                    host = parsed.hostname or ""
                    if not any(s in host for s in skip_patterns) and "." in host:
                        score = _score_domain(host)
                        d = f"https://{host}"
                        if d not in candidates or score < candidates[d]:
                            candidates[d] = score
        except (json.JSONDecodeError, OSError):
            pass

    # Check ssl.json for primary domain
    ssl_path = SOURCES_DIR / slug / "ssl.json"
    if ssl_path.exists():
        try:
            ssl_data = json.loads(ssl_path.read_text())
            primary = ssl_data.get("primary_domain", "")
            if primary:
                d = f"https://{primary}"
                candidates[d] = min(candidates.get(d, 99), 1)
        except (json.JSONDecodeError, OSError):
            pass

    # Common pattern guesses (only set if not already found with better score)
    name_clean = re.sub(r'[^a-z0-9\s]', '', company_lower)
    words = name_clean.split()
    if words:
        nospaces = slug.replace("-", "")
        for suffix in [".org", ".com", ".us"]:
            d = f"https://www.{nospaces}{suffix}"
            candidates[d] = min(candidates.get(d, 99), 8)
        candidates.setdefault(f"https://{nospaces}.org", 9)
        if "public schools" in company_lower or "school" in company_lower:
            abbrev = "".join(w[0] for w in words if len(w) > 2)
            if abbrev:
                candidates.setdefault(f"https://www.{abbrev}.org", 7)
                candidates.setdefault(f"https://www.{abbrev}.k12.ga.us", 7)

    # Sort by score (best first), deduplicate
    sorted_domains = sorted(candidates.items(), key=lambda x: x[1])
    return [d.rstrip("/") for d, _ in sorted_domains[:8]]


def _scrape_leadership_page(base_url: str, paths: list[str]) -> list[dict]:
    """Try multiple leadership page paths, return extracted people."""
    if not BeautifulSoup:
        return []

    people = []
    tried = 0

    for path in paths:
        if tried >= 6:  # Cap attempts
            break
        url = urljoin(base_url + "/", path.lstrip("/"))
        tried += 1

        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                                allow_redirects=True)
            if resp.status_code != 200:
                continue
            if len(resp.text) < 500:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Remove script/style
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()

            text = soup.get_text(separator="\n", strip=True)

            # Simple heuristic: look for name+title patterns
            extracted = _extract_people_from_html(soup, url)
            if extracted:
                people.extend(extracted)
                break  # Found a good page, stop trying

            time.sleep(0.5)
        except (requests.RequestException, Exception):
            continue

    return people


def _extract_people_from_html(soup, source_url: str) -> list[dict]:
    """Extract people from HTML using common structural patterns."""
    people = []
    seen_names = set()

    # Title keywords for detection
    title_kw = {"superintendent", "director", "chief", "officer", "president",
                "vice president", "vp", "dean", "provost", "manager", "chair",
                "secretary", "treasurer", "member", "coordinator", "head",
                "executive", "cio", "cto", "cfo", "ciso", "cso", "assistant",
                "associate"}

    def _clean_text(t):
        t = re.sub(r'(?:Click to Email|Email|Phone|Fax|Office of)\b.*$', '', t).strip()
        t = re.sub(r'\(\d{3}\)\s*\d{3}[-.]?\d{4}.*$', '', t).strip()
        return t

    def _looks_like_name(text):
        words = text.split()
        if not (2 <= len(words) <= 5):
            return False
        if not all(w[0].isupper() for w in words if len(w) > 1 and w[0].isalpha()):
            return False
        skip = {"board", "district", "school", "office", "department", "about",
                "leadership", "administration", "contact", "our", "team", "meet",
                "staff", "welcome", "mission", "vision", "senior", "areas",
                "responsibility", "email"}
        if any(w.lower() in skip for w in words):
            return False
        return True

    def _is_title_text(text):
        return any(kw in text.lower() for kw in title_kw)

    # Pattern 1: Look for structured cards/sections with name + title
    for container in soup.find_all(["div", "li", "article", "section"]):
        heading = container.find(["h2", "h3", "h4", "strong", "b"])
        if not heading:
            continue

        heading_text = _clean_text(heading.get_text(strip=True))
        if not heading_text or len(heading_text) > 120:
            continue

        # Gather nearby text
        siblings = []
        for sib in container.find_all(["p", "span", "em", "div"]):
            sib_text = _clean_text(sib.get_text(strip=True))
            if sib_text and sib_text != heading_text and len(sib_text) < 150:
                siblings.append(sib_text)

        name = None
        title = None

        # Case A: Heading is a person name, title is in siblings
        if _looks_like_name(heading_text) and not _is_title_text(heading_text):
            name = heading_text
            for sib_text in siblings[:3]:
                if _is_title_text(sib_text):
                    title = sib_text
                    break

        # Case B: Heading is a title, name is in siblings or in heading itself
        # Common on K-12/gov pages: <h3>Chief Technology Officer</h3><p>John Smith</p>
        elif _is_title_text(heading_text):
            title = heading_text
            # Check if heading also contains "Name, Title" pattern
            # e.g., "Dr. Bryan Johnson, Superintendent"
            comma_match = re.match(r'^((?:Dr\.\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*[,]\s*(.+)$', heading_text)
            if comma_match:
                name = comma_match.group(1)
                title = comma_match.group(2)
            else:
                # Look for name in sibling text
                for sib_text in siblings[:5]:
                    # Try to extract name from "NameTitle" concatenation
                    # e.g., "Travis NorvellChief Strategy Officer"
                    title_pos = sib_text.lower().find(title.lower()[:20]) if len(title) > 5 else -1
                    if title_pos > 3:
                        candidate_name = sib_text[:title_pos].strip()
                        if _looks_like_name(candidate_name):
                            name = candidate_name
                            break
                    elif _looks_like_name(sib_text):
                        name = sib_text
                        break

        if not name or not title:
            continue

        # Clean: if title starts with the name, strip it
        if title.startswith(name):
            title = title[len(name):].strip()
        # Clean remaining junk from title
        title = _clean_text(title)
        if not title:
            continue
        name = _clean_text(name)
        # Skip junk entries: too short, starts with title word, or contains lowercase-uppercase boundary issues
        if len(name) < 4 or name.lower().startswith("program"):
            continue
        # Skip if name contains title keywords (it's likely a title extracted as a name)
        name_lower = name.lower()
        if any(kw in name_lower for kw in ["president", "director", "superintendent",
                                            "officer", "chief", "dean", "provost"]):
            continue

        name_key = name.lower()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)

        people.append({
            "name": name,
            "title": title,
            "source_url": source_url,
            "extraction_method": "html_structure",
        })

    # Pattern 2: If structured extraction found nothing, try regex on text
    if not people:
        text = soup.get_text(separator="\n", strip=True)
        # Look for "Name, Title" or "Name — Title" patterns
        pattern = r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*[,—–\-|]\s*((?:Superintendent|Director|Chief|Officer|President|Vice\s+President|VP|Dean|Provost|CIO|CTO|CFO|CISO|CSO|Chair|Board\s+Member)[^.\n]{0,80})'
        for m in re.finditer(pattern, text):
            name = m.group(1).strip()
            title = m.group(2).strip()
            name_key = name.lower()
            if name_key not in seen_names:
                seen_names.add(name_key)
                people.append({
                    "name": name,
                    "title": title,
                    "source_url": source_url,
                    "extraction_method": "regex",
                })

    return people[:20]  # Cap at 20


# ---------------------------------------------------------------------------
# Role classification
# ---------------------------------------------------------------------------

ROLE_PATTERNS = {
    "Executive": [
        r"superintendent", r"president", r"ceo", r"chief\s+executive",
        r"chancellor", r"provost",
    ],
    "IT": [
        r"cio", r"chief\s+information", r"chief\s+technology", r"cto",
        r"it\s+director", r"director.*(?:technology|information|it\b)",
        r"vp.*(?:technology|information|it\b)", r"chief\s+digital",
    ],
    "Security": [
        r"ciso", r"cso", r"chief\s+security", r"director.*security",
        r"vp.*security", r"dean.*(?:safety|security|public\s+safety)",
        r"security\s+director", r"chief.*safety",
    ],
    "Operations": [
        r"coo", r"chief\s+operating", r"director.*operations",
        r"vp.*operations", r"director.*facilities", r"facilities",
    ],
    "Facilities": [
        r"director.*facilities", r"facilities\s+(?:director|manager)",
        r"vp.*facilities", r"chief.*facilities",
    ],
    "Board": [
        r"board\s+(?:member|chair|president|vice)", r"trustee",
        r"school\s+board", r"regent",
    ],
}


def _classify_role(title: str) -> str:
    title_lower = title.lower()
    for role, patterns in ROLE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, title_lower):
                return role
    return "Other"


# ---------------------------------------------------------------------------
# LinkedIn search URL generation
# ---------------------------------------------------------------------------

def _linkedin_search_url(name: str, title: str, company: str) -> str:
    keywords = f"{name} {title} {company}"
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(keywords)}"


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------

def fetch_leadership_data(
    company_name: str,
    entity_type: str = "unknown",
    force_refresh: bool = False,
) -> dict:
    """Fetch named leadership individuals for a company.

    Args:
        company_name: Company/entity name
        entity_type: One of k12_district, higher_ed, public_corporation, etc.
        force_refresh: Force re-fetch even if cache is fresh
    """
    slug = _slugify(company_name)
    cache = _cache_path(company_name)

    if not force_refresh and _cache_is_fresh(cache):
        try:
            return json.loads(cache.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    named_individuals = []
    extraction_sources = []

    # Layer 1a: Extract names from news.json
    news_people = _extract_names_from_news(company_name, slug)
    for p in news_people:
        named_individuals.append({
            "name": p.get("name", ""),
            "title": p.get("title", ""),
            "role_classification": _classify_role(p.get("title", "")),
            "tenure_estimate": None,
            "recent_activity": [{
                "type": "news_mention",
                "quote_excerpt": p.get("quote_excerpt"),
                "date": p.get("date"),
                "context": p.get("context", ""),
                "source_url": p.get("source_url", ""),
            }],
            "source_urls": [p.get("source_url", "")],
            "extraction_method": "news_haiku",
            "linkedin_search_url": _linkedin_search_url(
                p.get("name", ""), p.get("title", ""), company_name
            ),
        })
    if news_people:
        extraction_sources.append("news.json")

    # Layer 1b: Scrape leadership pages
    domains = _guess_domain(company_name, slug)

    # Choose paths by entity type
    if entity_type in ("k12_district", "k12"):
        paths = LEADERSHIP_PATHS_K12
    elif entity_type == "higher_ed":
        paths = LEADERSHIP_PATHS_HIGHER_ED
    else:
        paths = LEADERSHIP_PATHS_GENERAL

    scraped_people = []
    for domain in domains[:4]:  # Try up to 4 domains
        scraped = _scrape_leadership_page(domain, paths)
        if scraped:
            scraped_people.extend(scraped)
            break  # Found results, stop trying domains

    # Merge scraped people, deduplicating against news-extracted names
    existing_names = {ind["name"].lower() for ind in named_individuals}
    for p in scraped_people:
        name_lower = p["name"].lower()
        if name_lower in existing_names:
            # Merge: add source URL to existing entry
            for ind in named_individuals:
                if ind["name"].lower() == name_lower:
                    if p["source_url"] not in ind["source_urls"]:
                        ind["source_urls"].append(p["source_url"])
                    break
        else:
            existing_names.add(name_lower)
            named_individuals.append({
                "name": p["name"],
                "title": p["title"],
                "role_classification": _classify_role(p["title"]),
                "tenure_estimate": None,
                "recent_activity": [],
                "source_urls": [p["source_url"]],
                "extraction_method": p.get("extraction_method", "html_scrape"),
                "linkedin_search_url": _linkedin_search_url(
                    p["name"], p["title"], company_name
                ),
            })
    if scraped_people:
        extraction_sources.append("leadership_page")

    # Build output
    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "leadership_discovery",
        "company": company_name,
        "entity_type": entity_type,
        "extraction_sources": extraction_sources,
        "domains_tried": domains[:4],
        "named_individuals": named_individuals,
        "status": "ok" if named_individuals else "insufficient_data",
        "fragility_note": (
            "Leadership page scraping is inherently fragile. Pages behind JS "
            "frameworks, login walls, or non-standard paths may not be captured. "
            "News extraction depends on news.json quality. LinkedIn URLs are "
            "search templates — not validated against real profiles."
        ),
    }

    # Write cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, indent=2, default=str))

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python leadership.py <company_name> [entity_type]", file=sys.stderr)
        sys.exit(1)

    company = sys.argv[1]
    etype = sys.argv[2] if len(sys.argv) > 2 else "unknown"
    result = fetch_leadership_data(company, entity_type=etype, force_refresh=True)
    print(json.dumps(result, indent=2))
    print(f"\nFound {len(result['named_individuals'])} named individuals", file=sys.stderr)
