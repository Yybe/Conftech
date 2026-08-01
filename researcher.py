"""researcher.py - The Deep Researcher agent.

Crawls each raw lead's website (ScrapeGraphAI when installed, BeautifulSoup
fallback otherwise) and extracts: mission, pain points, contact emails, and a
summary. Findings are saved to leads.scraped_data and status -> 'researched'.

Extraction goal comes from /prompts/researcher.md.
HTML pages are disk-cached (30-day TTL) to cut bandwidth and rate limits.
"""

from __future__ import annotations

import os
import re
from typing import Any

import requests
from dotenv import load_dotenv

import cache
import database as db
from llm import load_prompt

load_dotenv()

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+", re.IGNORECASE)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
CONTACT_PATHS = ("/contact", "/contact-us", "/about", "/about-us")


def _scrapegraphai_extract(url: str, prompt: str) -> dict[str, Any]:
    """Primary path: ScrapeGraphAI SmartScraperGraph via OpenRouter (Gemma)."""
    try:
        from scrapegraphai.graphs import SmartScraperGraph
    except ImportError as exc:
        raise RuntimeError(f"scrapegraphai not installed: {exc}") from exc

    graph_config = {
        "llm": {
            "api_key": os.getenv("OPENROUTER_API_KEY", ""),
            "model": os.getenv("LLM_MODEL", "google/gemma-4-26b-a4b-it:free"),
            "base_url": "https://openrouter.ai/api/v1",
            "temperature": 0.2,
        },
        "verbose": False,
        "headless": True,
    }
    graph = SmartScraperGraph(prompt=prompt, source=url, config=graph_config)
    result = graph.run()
    if not isinstance(result, dict) or not result:
        raise RuntimeError(f"ScrapeGraphAI returned nothing usable: {result!r}")
    return result


def _fetch_page(url: str) -> str:
    """Fetch a page's HTML with caching (namespace 'pages', TTL 30 days)."""
    hit = cache.get("pages", {"url": url}, ttl_days=30)
    if hit is not None:
        return hit
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    text = resp.text
    cache.set("pages", {"url": url}, text)
    return text


def _fallback_extract(url: str) -> dict[str, Any]:
    """Secondary path: plain HTML parsing (no extra dependencies)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_fetch_page(url), "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    title = (soup.title.get_text(strip=True) if soup.title else "") or ""
    meta_desc = ""
    tag = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    if tag and tag.get("content"):
        meta_desc = tag["content"].strip()

    visible = soup.get_text(" ", strip=True)
    visible = re.sub(r"\s+", " ", visible)

    emails: set[str] = set()
    for mailto in soup.select('a[href^="mailto:"]'):
        addr = mailto["href"].replace("mailto:", "").split("?")[0]
        if EMAIL_RE.fullmatch(addr):
            emails.add(addr.lower())
    for m in EMAIL_RE.findall(visible):
        if not m.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            emails.add(m.lower())

    # Scan a contact/about page for more emails
    for path in CONTACT_PATHS:
        try:
            sub = BeautifulSoup(_fetch_page(url.rstrip("/") + path), "lxml")
            for tag in sub(["script", "style", "noscript"]):
                tag.decompose()
            for m in EMAIL_RE.findall(sub.get_text(" ", strip=True)):
                if not m.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
                    emails.add(m.lower())
        except Exception:
            continue

    summary = (meta_desc or visible)[:400].strip()
    return {
        "mission": title,
        "pain_points": [],
        "contact_emails": sorted(emails),
        "product_mentions": [],
        "summary": summary,
        "extractor": "bs4-fallback",
    }


def research_one(lead: dict[str, Any]) -> dict[str, Any]:
    """Crawl one lead's website and persist findings."""
    url = lead.get("website")
    if not url:
        raise ValueError(f"Lead {lead['id']} has no website")

    prompt = load_prompt("researcher.md") + (
        "\n\nWebsite to analyze: " + url
    )

    try:
        try:
            findings = _scrapegraphai_extract(url, prompt)
            findings.setdefault("extractor", "scrapegraphai")
        except Exception as exc:
            print(f"[researcher] ScrapeGraphAI failed for {url} ({exc}); using fallback.")
            findings = _fallback_extract(url)
        return db.update_lead(
            lead["id"],
            scraped_data=findings,
            contact_email=(findings.get("contact_emails") or [None])[0],
            status=db.RESEARCHED,
        )
    except Exception as exc:
        db.update_lead(lead["id"], status=db.ERROR)
        raise RuntimeError(f"Research failed for {url}: {exc}") from exc


def research(status: str = db.RAW, limit: int = 20) -> list[dict[str, Any]]:
    """Process all leads in `status` (default: raw)."""
    leads = db.get_leads_by_status(status, limit=limit)
    print(f"[researcher] Processing {len(leads)} lead(s) with status={status}")
    done: list[dict[str, Any]] = []
    for lead in leads:
        try:
            updated = research_one(lead)
            done.append(updated)
            print(f"[researcher] {updated.get('company_name')} -> researched")
        except Exception as exc:
            print(f"[researcher] ERROR {lead.get('company_name')}: {exc}")
    return done


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deep-research raw leads")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--status", default=db.RAW)
    args = parser.parse_args()
    research(status=args.status, limit=args.limit)
