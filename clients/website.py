"""
Website Client — Scrape the company's own website for basic company info.

Works for any entity with a website. Provides a universal data floor
so the company-bg subagent can build a snapshot even when SEC/NCES/SAM
return nothing.

Domain discovery: uses Tavily search to find the company's primary domain,
then fetches homepage + common leadership/about pages.

Cache TTL: 30 days (company websites don't change frequently).
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "sources"

TAVILY_API_URL = "https://api.tavily.com/search"
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

CACHE_TTL_DAYS = 30

# Pages to try on the discovered domain
LEADERSHIP_PATHS = [
    "/", "/about", "/about-us", "/leadership", "/team",
    "/executives", "/our-company", "/company", "/about/leadership",
    "/about/team", "/who-we-are",
]

# Domains to skip when searching for the company's own site
SKIP_DOMAINS = {
    "wikipedia.org", "linkedin.com", "facebook.com", "twitter.com",
    "instagram.com", "youtube.com", "glassdoor.com", "indeed.com",
    "bloomberg.com", "reuters.com", "crunchbase.com", "yelp.com",
    "bbb.org", "google.com", "apple.com", "amazon.com",
    "reddit.com", "yahoo.com", "msn.com",
}

REQUEST_TIMEOUT = 10
MAX_PAGE_SIZE = 100_000  # 100KB text limit per page


class _TextExtractor(HTMLParser):
    """Strip HTML tags, return visible text."""

    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip = False
        self._skip_tags = {"script", "style", "noscript", "svg", "head"}

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip = True

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self._parts.append(text)

    def get_text(self):
        return "\n".join(self._parts)


def _html_to_text(html: str) -> str:
    """Extract visible text from HTML."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = parser.get_text()
    # Collapse excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:MAX_PAGE_SIZE]


def _find_domain(company_name: str) -> str | None:
    """Use Tavily to find the company's primary website domain."""
    if not TAVILY_API_KEY:
        return None

    try:
        resp = requests.post(
            TAVILY_API_URL,
            json={
                "api_key": TAVILY_API_KEY,
                "query": f"{company_name} official website",
                "search_depth": "basic",
                "max_results": 5,
                "include_domains": [],
                "exclude_domains": list(SKIP_DOMAINS),
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None

        results = resp.json().get("results", [])
        # Score results by how much the domain matches the company name
        company_words = set(re.sub(r'[^a-z0-9\s]', '', company_name.lower()).split())
        company_words -= {"the", "of", "and", "for", "in", "at", "a", "an", "inc", "corp", "llc", "ltd"}

        best_domain = None
        best_score = -1

        for r in results:
            url = r.get("url", "")
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower().replace("www.", "")
            if not host or any(s in host for s in SKIP_DOMAINS):
                continue
            # Score: how many company name words appear in the domain
            domain_base = host.split(".")[0]
            score = sum(1 for w in company_words if w in domain_base and len(w) > 2)
            if score > best_score:
                best_score = score
                best_domain = host

        # If no word matches, just use the first non-skipped result
        if not best_domain and results:
            for r in results:
                parsed = urlparse(r.get("url", ""))
                host = (parsed.hostname or "").lower().replace("www.", "")
                if host and not any(s in host for s in SKIP_DOMAINS):
                    best_domain = host
                    break

        return best_domain
    except Exception:
        return None


def _fetch_page(url: str) -> str | None:
    """Fetch a single page, return text content or None."""
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; VerkadaResearch/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type and "text" not in content_type:
            return None
        return _html_to_text(resp.text)
    except Exception:
        return None


def fetch_website_data(company_name: str, force_refresh: bool = False) -> dict:
    """Fetch and parse the company's website.

    Returns structured data with domain, scraped page content, and metadata.
    """
    slug = re.sub(r'[^a-z0-9]+', '-', company_name.lower()).strip('-')
    cache_path = SOURCES_DIR / slug / "website.json"

    # Check cache
    if cache_path.exists() and not force_refresh:
        try:
            cached = json.loads(cache_path.read_text())
            age_days = (time.time() - cache_path.stat().st_mtime) / 86400
            if age_days < CACHE_TTL_DAYS:
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    # Find domain
    domain = _find_domain(company_name)
    if not domain:
        result = {
            "status": "no_domain_found",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "company_name": company_name,
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(result, indent=2))
        return result

    # Fetch pages
    scraped = {}
    for path in LEADERSHIP_PATHS:
        url = f"https://{domain}{path}"
        text = _fetch_page(url)
        if text and len(text) > 100:  # Skip trivially short pages
            scraped[url] = text[:MAX_PAGE_SIZE]
        time.sleep(0.5)  # Be polite

    result = {
        "status": "ok" if scraped else "no_pages_accessible",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "company_name": company_name,
        "domain": domain,
        "pages_fetched": len(scraped),
        "pages": scraped,
    }

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result, indent=2, default=str))
    return result
