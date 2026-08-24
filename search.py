"""search.py - Zero-auth web search with DuckDuckGo (ddgs) and Serper fallback.

Provides a unified, cached web search interface that works without any API keys
or paid subscriptions, avoiding rate limits.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any

import requests
from dotenv import load_dotenv

import cache

load_dotenv()

SERPER_URL = "https://google.serper.dev/search"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+", re.IGNORECASE)

# Domains that are directories, social media, or data aggregators (not company websites)
SKIP_DOMAINS = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "crunchbase.com", "glassdoor.com", "wikipedia.org",
    "zoominfo.com", "dnb.com", "hoovers.com", "bloomberg.com", "forbes.com",
    "buzzfile.com", "bizapedia.com", "data.leadiq.com", "leadiq.com", "craft.co",
    "theorg.com", "rocketreach.co", "apollo.io", "signalhire.com",
    "linkedin.cn", "yelp.com", "trustpilot.com", "pitchbook.com",
    "g2.com", "capterra.com", "indeed.com", "yellowpages.com"
)


def _search_ddgs(query: str, max_results: int = 10) -> list[dict[str, str]]:
    """Query DuckDuckGo using ddgs / duckduckgo_search."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        ddgs = DDGS()
        raw_results = list(ddgs.text(query, max_results=max_results))
        results = []
        for r in raw_results:
            results.append({
                "title": str(r.get("title") or ""),
                "link": str(r.get("href") or r.get("link") or ""),
                "snippet": str(r.get("body") or r.get("snippet") or ""),
            })
        return results
    except Exception as exc:
        print(f"[search] DuckDuckGo search error for '{query}': {exc}")
        return []


def _search_serper(query: str, max_results: int = 10) -> list[dict[str, str]]:
    """Query Serper Google search API if API key is present."""
    api_key = os.getenv("SERPER_API_KEY", "").strip()
    if not api_key:
        return []

    try:
        resp = requests.post(
            SERPER_URL,
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": max_results},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            results = []
            for item in data.get("organic", []):
                results.append({
                    "title": str(item.get("title") or ""),
                    "link": str(item.get("link") or ""),
                    "snippet": str(item.get("snippet") or ""),
                })
            return results
        else:
            print(f"[search] Serper HTTP {resp.status_code}: {resp.text[:200]}")
            return []
    except Exception as exc:
        print(f"[search] Serper search error for '{query}': {exc}")
        return []


@cache.cached("search", ttl_days=30)
def web_search(query: str, max_results: int = 10) -> dict[str, Any]:
    """Unified search: queries DuckDuckGo first (zero-auth/no rate limit),

    falls back to Serper if configured, returns Serper-compatible dict:
    {"query": query, "organic": [{"title": ..., "link": ..., "snippet": ...}]}
    """
    query = query.strip()
    if not query:
        return {"query": "", "organic": []}

    # 1. Try DuckDuckGo
    results = _search_ddgs(query, max_results=max_results)

    # 2. Fallback to Serper if DDGS yielded no results
    if not results and os.getenv("SERPER_API_KEY"):
        print(f"[search] DDGS returned 0 results for '{query}', trying Serper fallback")
        results = _search_serper(query, max_results=max_results)

    return {
        "query": query,
        "organic": results,
    }


# Backwards compatibility alias for existing code
serper_search = web_search


# Common placeholder names found in format-specifier snippets (e.g. RocketReach)
_PLACEHOLDER_LOCALPARTS = {
    "first.last", "first_last", "firstlast", "first-last", "flast", "first.l",
    "john.doe", "jane.doe", "johndoe", "janedoe", "jdoe", "j.doe", "john.smith",
    "jsmith", "yourname", "name", "username", "email", "example", "test",
    "sample", "user", "someone", "d_j", "last", "first",
}


def extract_emails_from_text(text: str) -> list[str]:
    """Extract and deduplicate valid email addresses from text blobs,
    filtering out placeholder examples (e.g. first.last@domain.com).
    """
    seen: set[str] = set()
    for m in EMAIL_RE.findall(text):
        lower = m.lower().strip(".,;:()")
        if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")):
            continue
        local_part = lower.split("@")[0] if "@" in lower else ""
        if local_part in _PLACEHOLDER_LOCALPARTS:
            continue
        if len(local_part) < 2 or "." not in lower.split("@")[-1]:
            continue
        seen.add(lower)
    return sorted(seen)


def search_contacts_for_domain(domain: str, company_name: str = "") -> list[str]:
    """Search the web for public contact emails associated with a domain/company."""
    domain = domain.strip().lower()
    if not domain:
        return []

    queries = [
        f'"{domain}" contact email',
        f'"{domain}" email',
    ]
    if company_name and company_name.lower() != domain:
        clean_name = company_name.split(" - ")[0].split(" | ")[0].strip()
        queries.append(f'"{clean_name}" "{domain}" email')

    emails: set[str] = set()
    for q in queries:
        try:
            sr = web_search(q, max_results=5)
            for item in sr.get("organic", []) or []:
                text = f"{item.get('title', '')} {item.get('snippet', '')}"
                found = extract_emails_from_text(text)
                for email in found:
                    # Keep if matches company domain or has valid business domain
                    emails.add(email)
        except Exception as exc:
            print(f"[search] Contact search failed for query '{q}': {exc}")

    return sorted(emails)


def resolve_official_website(company_name: str) -> str | None:
    """Find the official website domain for a given company name."""
    clean_name = company_name.split(" - ")[0].split(" | ")[0].strip()
    queries = [
        f'{clean_name} official website',
        f'"{clean_name}" official website',
    ]
    for query in queries:
        try:
            sr = web_search(query, max_results=8)
            for item in sr.get("organic", []) or []:
                link = str(item.get("link") or "")
                host = urllib.parse.urlparse(link).netloc.lower()
                if not host:
                    continue
                if any(s in host for s in SKIP_DOMAINS):
                    continue
                if host.startswith("www."):
                    host = host[4:]
                if host and "." in host:
                    return host
        except Exception as exc:
            print(f"[search] Domain resolution failed for '{query}': {exc}")
    return None
