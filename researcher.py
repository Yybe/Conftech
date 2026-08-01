"""researcher.py - The Deep Researcher agent.

Crawls each raw lead's website with a 3-tier extraction strategy and persists
findings to leads.scraped_data (status -> 'researched'):

  1. ScrapeGraphAI - real browser rendering + prompt-driven extraction
  2. BS4 + LLM     - page text cleaned with BeautifulSoup, analyzed by Gemma
  3. BS4 raw       - plain HTML parsing (always available, never raises)

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
from llm import chat, load_prompt, parse_json

load_dotenv()

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+", re.IGNORECASE)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
CONTACT_PATHS = ("/contact", "/contact-us", "/about", "/about-us")
RESEARCH_KEYS = ("mission", "pain_points", "contact_emails", "product_mentions", "summary")
MAX_PAGE_TEXT = 6000


# --- Tier 1: ScrapeGraphAI (real browser rendering) --------------------------

def _scrapegraphai_extract(url: str, prompt: str) -> dict[str, Any]:
    """Render the page in a real browser (JS included), then analyze the text.

    SmartScraperGraph's own GenerateAnswer node short-circuits on free OpenRouter
    models (returns garbage / 'no content'), so we use its Fetch+Parse nodes for
    the browser rendering and run our own proven extraction prompt on the result.
    """
    import scrapegraphai_compat

    scrapegraphai_compat.patch()  # ChatOllama shim for langchain-community 0.4+
    try:
        from scrapegraphai.nodes import FetchNode, ParseNode
    except ImportError as exc:
        raise RuntimeError(f"scrapegraphai not installed: {exc}") from exc

    node_config = {"headless": True, "chunk_size": 8000}
    fetch = FetchNode(input="url", output="html", node_config=node_config)
    parse = ParseNode(input="h", output="parsed_doc", node_config=node_config)
    state = fetch.execute({"url": url, "user_prompt": prompt})
    state = parse.execute(state)
    docs = state.get("parsed_doc") or []
    text = " ".join(str(d) for d in docs)
    if not text.strip():
        raise RuntimeError("ScrapeGraphAI rendered the page but extracted no text")
    return _analyze_text(url, text, prompt, "scrapegraphai")


# --- Page fetching / cleaning -------------------------------------------------

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


def _clean_text(html: str) -> str:
    """Strip scripts/styles and return collapsed visible text."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def _scan_emails(url: str) -> list[str]:
    """Collect emails from the homepage and a few contact/about pages."""
    emails: set[str] = set()
    pages = [url] + [url.rstrip("/") + p for p in CONTACT_PATHS]
    for page in pages:
        try:
            text = _clean_text(_fetch_page(page))
        except Exception:  # noqa: BLE001 - any page failure is non-fatal
            continue
        for m in EMAIL_RE.findall(text):
            if not m.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
                emails.add(m.lower())
    return sorted(emails)


def _normalize_findings(findings: dict) -> dict[str, Any]:
    """Keep only known keys with safe types."""
    out: dict[str, Any] = {}
    for key in RESEARCH_KEYS:
        value = findings.get(key)
        if key in ("pain_points", "contact_emails", "product_mentions"):
            out[key] = [str(v) for v in value] if isinstance(value, list) else []
        else:
            out[key] = str(value or "")
    return out


def _finalize(url: str, findings: dict[str, Any], extractor: str) -> dict[str, Any]:
    """Normalize findings and merge regex-scanned emails (more reliable than LLM)."""
    out = _normalize_findings(findings)
    emails = set(out["contact_emails"])
    emails.update(_scan_emails(url))
    out["contact_emails"] = sorted(emails)
    if not out["summary"]:
        try:
            out["summary"] = _clean_text(_fetch_page(url))[:300]
        except Exception:  # noqa: BLE001
            pass
    out["extractor"] = extractor
    return out


def _analyze_text(url: str, text: str, prompt: str, extractor: str) -> dict[str, Any]:
    """Gemma analysis of already-obtained page text (shared by tiers 1 & 2)."""
    if len(text) < 60:
        raise RuntimeError("page has too little visible text for LLM analysis")

    user = (
        prompt
        + f"\n\nWebsite: {url}\n\nPAGE TEXT (trimmed to {MAX_PAGE_TEXT} chars):\n\n"
        + text[:MAX_PAGE_TEXT]
    )
    reply = chat(
        system="You are a meticulous research extraction engine. Follow the goal exactly.",
        user=user,
        max_tokens=800,
    )
    return _finalize(url, parse_json(reply), extractor)


# --- Tier 2: BS4 + Gemma (no browser, but LLM-level analysis) -----------------

def _bs4_gemma_extract(url: str, prompt: str) -> dict[str, Any]:
    """Clean the page text with BeautifulSoup, then have Gemma analyze it."""
    return _analyze_text(url, _clean_text(_fetch_page(url)), prompt, "bs4+gemma")


# --- Tier 3: BS4 raw (no LLM, always available) ------------------------------

def _fallback_extract(url: str) -> dict[str, Any]:
    """Plain HTML parsing - the always-works safety net."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_fetch_page(url), "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    title = (soup.title.get_text(strip=True) if soup.title else "") or ""
    meta_desc = ""
    tag = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    if tag and tag.get("content"):
        meta_desc = tag["content"].strip()

    visible = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    summary = (meta_desc or visible)[:400].strip()
    return {
        "mission": title,
        "pain_points": [],
        "contact_emails": _scan_emails(url),
        "product_mentions": [],
        "summary": summary,
        "extractor": "bs4-fallback",
    }


# --- Orchestration ------------------------------------------------------------

def research_one(lead: dict[str, Any]) -> dict[str, Any]:
    """Crawl one lead's website and persist findings."""
    url = lead.get("website")
    if not url:
        raise ValueError(f"Lead {lead['id']} has no website")

    prompt = load_prompt("researcher.md") + "\n\nWebsite to analyze: " + url

    try:
        try:
            findings = _finalize(url, _scrapegraphai_extract(url, prompt), "scrapegraphai")
        except Exception as exc:
            print(f"[researcher] ScrapeGraphAI unavailable for {url} ({exc}); trying BS4+LLM.")
            try:
                findings = _bs4_gemma_extract(url, prompt)
            except Exception as exc2:
                print(f"[researcher] BS4+LLM failed for {url} ({exc2}); using raw BS4 fallback.")
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
