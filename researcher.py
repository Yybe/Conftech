"""researcher.py - The Deep Researcher agent.

Crawls each raw lead's website (homepage + contact/about pages + prioritized
internal pages, up to MAX_CRAWL_PAGES) with a 3-tier extraction strategy and
persists findings to leads.scraped_data (status -> 'researched'):

  1. ScrapeGraphAI - real browser rendering of the homepage + BS4+LLM on the rest
  2. BS4 + LLM     - every crawled page cleaned with BeautifulSoup, analyzed
  3. BS4 raw        - plain HTML parsing (always available, never raises)

Every finding is stored as an evidence entry with its source URL, page title,
and a verbatim quote matched from the page text, so the scorer and the UI can
show an auditable trail (claim -> page -> quote):

    scraped_data.evidence = [
        {"type": "pain_point", "text": "...", "url": "https://...",
         "quote": "...", "page_title": "..."},
        ...
    ]

Contact emails are ranked: decision-maker addresses (cto@, engineering@,
infrastructure@, security@, ...) sort first; generic boxes (info@, support@,
careers@) are kept but marked low priority.

Explicit buying signals (cloud migration, legacy system, technical debt, ...)
are detected deterministically in page text and stored as `buying_signal`
evidence so the scorer can weight them as the strongest fit signal.

Extraction goal comes from /prompts/researcher.md.
HTML pages are disk-cached (30-day TTL) to cut bandwidth and rate limits.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

import cache
import database as db
from llm import chat_json, load_prompt
from search import search_contacts_for_domain

load_dotenv()


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+", re.IGNORECASE)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
CONTACT_PATHS = ("/contact", "/contact-us", "/about", "/about-us")
MAX_PAGE_TEXT = 6000
MAX_CRAWL_PAGES = 8

# Contact email ranking -------------------------------------------------------
# Local-part keywords, ordered: best (decision-maker) -> functional -> generic.
# Decision-maker roles are what a salesperson can actually reach out to.
_DECISION_MAKER_KEYS = (
    "cto", "cio", "ceo", "coo", "chief", "director", "head", "vp", "owner",
    "founder", "partner", "engineering", "infra", "infrastructure", "cloud",
    "technology", "tech", "platform", "security", "operations", "ops", "devops",
)
_FUNCTIONAL_KEYS = (
    "sales", "marketing", "finance", "procurement", "purchasing", "accounts", "accounting",
)

# Explicit buying signals - much stronger than generic tech mentions. Detected
# deterministically in page text and scored as the top-weight signal.
BUYING_SIGNAL_PHRASES = (
    "legacy system", "legacy systems", "legacy infrastructure",
    "application migration", "app migration",
    "cloud migration", "cloud transformation",
    "digital transformation", "digitalization", "digitisation",
    "technical debt",
    "modernization", "modernisation", "modernise", "modernize",
    "lift and shift", "lift & shift",
    "end of life", "end-of-life",
    "upgrade infrastructure", "infrastructure upgrade",
    "migration services", "cloud migration services",
    "hybrid cloud",
)
# Phrase -> shorter label used when the matched sentence is too long.
_BUYING_SIGNAL_LABELS = {p: p for p in BUYING_SIGNAL_PHRASES}

_PRIORITY_KEYWORDS = (
    "about", "service", "solution", "platform", "product", "technology",
    "customer", "case", "client", "career", "news", "partner", "pricing",
    "why", "contact", "support", "help", "resource",
)
_SKIP_KEYWORDS = (
    "login", "signin", "signup", "register", "privacy", "terms", "legal",
    "cookie", "sitemap",
)
_SKIP_EXT = (
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".xml",
    ".json", ".zip", ".mp4", ".mp3", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx",
)
_STOPWORDS = {
    "the", "and", "for", "with", "our", "your", "that", "this", "from",
    "are", "was", "were", "has", "have", "had", "they", "their", "its",
    "not", "you", "we", "us", "can", "will", "would", "could", "should",
    "may", "might", "into", "over", "under", "about", "than", "then",
    "them", "who", "what", "when", "where", "which", "more", "most",
}

# Tech-stack detections that signal legacy/hybrid/cloud infrastructure. These are
# real TheirStack detections; the researcher turns them into `stack_signal`
# evidence entries (sourced to the company website) so the scorer can cite them.
_STACK_SIGNAL_HINTS = (
    "vmware", "hyperv", "mainframe", "windows-server", "windows-200", "windows-nt",
    "oracle", "sap", "ibm", "netapp", "mssql", "db2", "cobol", "solaris",
    "exchange", "sharepoint", "iis", "aws", "amazon-web-services", "azure",
    "google-cloud", "gcp", "kubernetes", "terraform", "cloudflare",
)


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


def _scan_page_emails(text: str) -> list[str]:
    """Deduplicated emails found in a page's visible text."""
    emails: set[str] = set()
    for m in EMAIL_RE.findall(text):
        if not m.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            emails.add(m.lower())
    return sorted(emails)


def _rank_email(email: str) -> tuple[int, int, str]:
    """Classify an email local-part: (priority 0=best, key index, role label).

    Decision-maker addresses (cto@, engineering@, infrastructure@, ...) rank
    first; functional boxes (sales@, procurement@, ...) second; generic boxes
    (info@, support@, careers@, ...) last - kept, but marked low priority.
    """
    local = email.split("@")[0].lower()
    for i, key in enumerate(_DECISION_MAKER_KEYS):
        if key in local:
            return (0, i, "decision-maker")
    for i, key in enumerate(_FUNCTIONAL_KEYS):
        if key in local:
            return (1, i, key)
    return (2, 0, "generic")


def rank_emails(emails_by_url: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Rank emails (best first), deduped, keeping the first page each was found on.

    Returns [{"email", "priority": high|medium|low, "role", "page_url", "page_title"}].
    """
    best: dict[str, dict[str, Any]] = {}
    for page_url, email in emails_by_url:
        priority, _idx, role = _rank_email(email)
        label = ("high", "medium", "low")[priority]
        prev = best.get(email)
        if prev is None or _rank_email(email) < _rank_email(prev["email"]):
            # keep first-seen page for the best priority of this email
            best[email] = {
                "email": email,
                "priority": label,
                "role": role,
                "page_url": page_url,
                "page_title": "",
            }
    ranked = sorted(best.values(), key=lambda r: (_rank_email(r["email"]), r["email"]))
    return ranked


def _sentence_with(raw: str, phrase: str) -> str:
    """First sentence of `raw` containing `phrase` (verbatim), or ''."""
    for sent in re.split(r"(?<=[.!?])\s+", raw):
        if phrase in sent.lower():
            return sent.strip()
    return ""


def _scan_buying_signals(pages: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Deterministic scan of crawled page text for explicit buying signals.

    Every match is stored as evidence: text/quote are the verbatim sentence
    from the page, url + page_title identify the source - nothing fabricated.
    """
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        raw = page.get("text") or ""
        low = raw.lower()
        for phrase in BUYING_SIGNAL_PHRASES:
            if phrase not in low:
                continue
            key = (phrase, page["url"])
            if key in seen:
                continue
            seen.add(key)
            sentence = _sentence_with(raw, phrase)
            if sentence and len(sentence) > 260:
                sentence = sentence[:257].rsplit(" ", 1)[0] + "…"
            found.append(
                {
                    "type": "buying_signal",
                    "text": sentence or f"{phrase} (mentioned on page)",
                    "url": page["url"],
                    "quote": sentence or "",
                    "page_title": page.get("title", ""),
                }
            )
    return found


# --- Multi-page crawl ---------------------------------------------------------

def _normalize_page_url(base: str, href: str) -> str | None:
    """Resolve an href to an absolute same-domain https URL, or None if unusable."""
    href = href.strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    try:
        url = urljoin(base, href)
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    base_netloc = urlparse(base).netloc.lower()
    netloc = parsed.netloc.lower()
    if netloc != base_netloc and not netloc.endswith("." + base_netloc):
        return None
    path = parsed.path.lower()
    if path.endswith(_SKIP_EXT) or any(k in path for k in _SKIP_KEYWORDS):
        return None
    return url.split("#")[0].split("?")[0]


def _discover_pages(url: str) -> list[str]:
    """Homepage + contact/about pages + prioritized internal links, capped.

    Contact/about pages come first because they hold the emails and mission.
    """
    pages: list[str] = [url]
    base = url.rstrip("/")
    for p in CONTACT_PATHS:
        u = base + p
        if u not in pages:
            pages.append(u)

    try:
        html = _fetch_page(url)
    except Exception:  # noqa: BLE001 - homepage fetch failure is non-fatal
        return pages[:MAX_CRAWL_PAGES]
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    candidates: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        u = _normalize_page_url(url, a["href"])
        if u and u not in pages:
            candidates.setdefault(u, urlparse(u).path.lower())

    ordered = sorted(
        candidates.items(),
        key=lambda kv: (0 if any(k in kv[1] for k in _PRIORITY_KEYWORDS) else 1, kv[0]),
    )
    for u, _path in ordered:
        if len(pages) >= MAX_CRAWL_PAGES:
            break
        pages.append(u)
    return pages[:MAX_CRAWL_PAGES]


def _crawl_pages(url: str) -> list[dict[str, str]]:
    """Fetch every discovered page. Returns [{url, title, meta, text}]."""
    pages: list[dict[str, str]] = []
    for page_url in _discover_pages(url):
        try:
            html = _fetch_page(page_url)
        except Exception:  # noqa: BLE001 - per-page failure is non-fatal
            continue
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        title = (soup.title.get_text(strip=True) if soup.title else "") or ""
        meta = ""
        tag = soup.find("meta", attrs={"name": re.compile("description", re.I)})
        if tag and tag.get("content"):
            meta = tag["content"].strip()
        for tag2 in soup(["script", "style", "noscript", "svg"]):
            tag2.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        pages.append({"url": page_url, "title": title, "meta": meta, "text": text})
    if not pages:
        raise RuntimeError(f"could not fetch any pages for {url}")
    return pages


# --- Verbatim quote matching --------------------------------------------------

def _match_quote(text: str, finding: str) -> str:
    """Return the verbatim sentence from `text` that best matches `finding`, or ''.

    The quote is always a real substring of the page text - never fabricated.
    Returns '' when no sentence is a good enough match.
    """

    def sig_words(s: str) -> set[str]:
        return {
            w for w in re.findall(r"[a-z0-9']+", s.lower())
            if len(w) > 2 and w not in _STOPWORDS
        }

    fw = sig_words(finding)
    if not fw:
        return ""
    best, best_score = "", 0
    for sent in re.split(r"(?<=[.!?])\s+", text):
        sw = sig_words(sent)
        if not sw:
            continue
        overlap = len(fw & sw)
        if overlap > best_score:
            best, best_score = sent.strip(), overlap
    if best_score >= 2 or best_score >= 0.6 * len(fw):
        return best
    return ""


# --- Per-page LLM analysis ----------------------------------------------------

def _analyze_page(url: str, text: str, prompt: str, page_title: str = "") -> dict[str, Any]:
    """LLM analysis of ONE page's text -> {'summary': str, 'evidence': [...]}.

    The LLM never provides URLs or quotes: the URL is this page's URL and the
    quote is matched verbatim from `text` in Python.
    """
    if len(text) < 60:
        raise RuntimeError("page has too little visible text for LLM analysis")

    user = (
        prompt
        + f"\n\nPage URL: {url}\n\nPAGE TEXT (trimmed to {MAX_PAGE_TEXT} chars):\n\n"
        + text[:MAX_PAGE_TEXT]
    )
    data = chat_json(
        system="You are a meticulous research extraction engine. Follow the goal exactly.",
        user=user,
        max_tokens=1500,
    )

    evidence: list[dict[str, Any]] = []
    for pp in data.get("pain_points", []) or []:
        if isinstance(pp, str) and pp.strip():
            evidence.append(
                {
                    "type": "pain_point",
                    "text": pp.strip(),
                    "url": url,
                    "quote": _match_quote(text, pp),
                    "page_title": page_title,
                }
            )
    for pm in data.get("product_mentions", []) or []:
        if isinstance(pm, str) and pm.strip():
            evidence.append(
                {
                    "type": "product_mention",
                    "text": pm.strip(),
                    "url": url,
                    "quote": _match_quote(text, pm),
                    "page_title": page_title,
                }
            )
    summary = str(data.get("summary", "") or "").strip()
    if summary:
        evidence.append(
            {
                "type": "summary",
                "text": summary,
                "url": url,
                "quote": _match_quote(text, summary),
                "page_title": page_title,
            }
        )

    if not evidence:
        raise RuntimeError(f"LLM found no usable findings on {url}")
    return {"summary": summary, "evidence": evidence}


# --- Tier 1: ScrapeGraphAI (real browser rendering) --------------------------

def _scrapegraphai_render(url: str) -> str:
    """Render the page in a real browser (JS included), return visible text.

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
    state = fetch.execute({"url": url, "user_prompt": "extract page text"})
    state = parse.execute(state)
    docs = state.get("parsed_doc") or []
    text = " ".join(str(d) for d in docs)
    if not text.strip():
        raise RuntimeError("ScrapeGraphAI rendered the page but extracted no text")
    return text


# --- Merge --------------------------------------------------------------------

def _finalize(
    url: str,
    pages: list[dict[str, str]],
    page_results: list[tuple[str, dict[str, Any]]],
    emails_by_url: list[tuple[str, str]],
    extractor: str,
) -> dict[str, Any]:
    """Merge per-page results + scanned emails into the final scraped_data shape."""
    evidence: list[dict[str, Any]] = []
    for _page_url, res in page_results:
        evidence.extend(res.get("evidence", []))

    # Deterministic explicit buying-signal scan over every crawled page.
    evidence.extend(_scan_buying_signals(pages))

    seen: set[tuple[str, str]] = set()
    uniq: list[dict[str, Any]] = []
    for ev in evidence:
        key = (ev.get("type", ""), ev.get("text", ""))
        if key not in seen:
            seen.add(key)
            uniq.append(ev)
    evidence = uniq

    pain_points = [e["text"] for e in evidence if e["type"] == "pain_point"]
    product_mentions = [e["text"] for e in evidence if e["type"] == "product_mention"]
    summaries = [e["text"] for e in evidence if e["type"] == "summary"]
    buying_signals = [e["text"] for e in evidence if e["type"] == "buying_signal"]

    # If no on-page emails were found, enrich via zero-auth web search
    if not emails_by_url:
        parsed_domain = urlparse(url).netloc.lower()
        if parsed_domain.startswith("www."):
            parsed_domain = parsed_domain[4:]
        try:
            discovered_emails = search_contacts_for_domain(parsed_domain)
            for em in discovered_emails:
                emails_by_url.append((url, em))
        except Exception as exc:
            print(f"[researcher] Contact search fallback failed for {url}: {exc}")

    # Ranked emails: decision-makers first, generics kept as low priority.
    title_by_url = {p["url"]: p.get("title", "") for p in pages}
    ranked = rank_emails(emails_by_url)

    for r in ranked:
        r["page_title"] = title_by_url.get(r["page_url"], "")
    contact_emails = [r["email"] for r in ranked]
    for r in ranked:
        evidence.append(
            {
                "type": "email",
                "text": r["email"],
                "url": r["page_url"],
                "quote": "",
                "page_title": r["page_title"],
                "email_priority": r["priority"],
                "email_role": r["role"],
            }
        )

    return {
        "mission": pages[0].get("title", "") if pages else "",
        "summary": summaries[0] if summaries else "",
        "pain_points": pain_points,
        "contact_emails": contact_emails,
        "contact_emails_ranked": ranked,
        "product_mentions": product_mentions,
        "buying_signals": buying_signals,
        "pages_crawled": [p["url"] for p in pages],
        "evidence": evidence,
        "extractor": extractor,
    }


def _stack_signal_evidence(lead: dict[str, Any], homepage: str, homepage_title: str = "") -> list[dict[str, Any]]:
    """TheirStack detections -> citable evidence entries (source = company site).

    These are real detections of legacy/cloud tech that the page text may
    never mention, so they get their own evidence type with an empty quote.
    """
    hits = [
        t for t in (lead.get("tech_stack") or [])
        if isinstance(t, str) and any(k in t.lower() for k in _STACK_SIGNAL_HINTS)
    ]
    return [
        {"type": "stack_signal", "text": f"runs {t}", "url": homepage, "quote": "", "page_title": homepage_title}
        for t in hits[:20]
    ]


# --- Orchestration ------------------------------------------------------------

def research_one(lead: dict[str, Any]) -> dict[str, Any]:
    """Crawl a lead's website (multi-page) and persist findings with evidence."""
    from webapp import _record_event
    url = lead.get("website")
    if not url:
        raise ValueError(f"Lead {lead['id']} has no website")

    prompt = load_prompt("researcher.md") + "\n\nWebsite to analyze: " + url
    _record_event(
        agent="researcher",
        kind="start",
        summary=f"Crawling {url}",
        detail={"url": url, "lead_id": lead.get("id"), "company": lead.get("company_name")},
        lead_id=lead.get("id"),
    )

    try:
        pages = _crawl_pages(url)
        _record_event(
            agent="researcher",
            kind="pages_crawled",
            summary=f"Crawled {len(pages)} pages on {url}",
            detail={"url": url, "pages": [p["url"] for p in pages]},
            lead_id=lead.get("id"),
        )
        emails_by_url = [
            (p["url"], email)
            for p in pages
            for email in _scan_page_emails(p["text"])
        ]
        _record_event(
            agent="researcher",
            kind="emails_found",
            summary=f"Found {len(emails_by_url)} on-page email mention(s)",
            detail={"count": len(emails_by_url), "emails": sorted(set(e for _, e in emails_by_url))[:20]},
            lead_id=lead.get("id"),
        )

        home_analysis: dict[str, Any] | None = None
        extractor = "bs4+gemma"
        try:
            _record_event(
                agent="researcher",
                kind="scrapegraphai_start",
                summary="Rendering homepage in headless browser (ScrapeGraphAI)",
                detail={"url": url},
                lead_id=lead.get("id"),
            )
            home_text = _scrapegraphai_render(url)
            home_analysis = _analyze_page(url, home_text, prompt, page_title=pages[0].get("title", ""))
            extractor = "scrapegraphai+bs4+gemma"
            _record_event(
                agent="researcher",
                kind="scrapegraphai_done",
                summary=f"Homepage analyzed: {len(home_analysis.get('evidence', []))} evidence",
                detail={"evidence_count": len(home_analysis.get('evidence', [])), "summary": home_analysis.get("summary", "")[:240]},
                lead_id=lead.get("id"),
            )
        except Exception as exc:
            print(f"[researcher] ScrapeGraphAI unavailable for {url} ({exc}); trying BS4+LLM.")
            _record_event(
                agent="researcher",
                kind="scrapegraphai_fallback",
                summary=f"ScrapeGraphAI unavailable ({exc}); falling back to BS4+LLM",
                detail={"error": str(exc)},
                lead_id=lead.get("id"),
            )

        page_results: list[tuple[str, dict[str, Any]]] = []
        for i, page in enumerate(pages):
            if i == 0 and home_analysis is not None:
                page_results.append((page["url"], home_analysis))
                continue
            _record_event(
                agent="researcher",
                kind="page_analyze",
                summary=f"LLM analysis: {page['url']}",
                detail={"url": page["url"], "title": page.get("title", "")},
                lead_id=lead.get("id"),
            )
            try:
                page_results.append(
                    (page["url"], _analyze_page(page["url"], page["text"], prompt, page_title=page.get("title", "")))
                )
                _record_event(
                    agent="researcher",
                    kind="evidence_added",
                    summary=f"Found {len(page_results[-1][1].get('evidence', []))} evidence on {page['url']}",
                    detail={"url": page["url"], "evidence": page_results[-1][1].get("evidence", [])[:4]},
                    lead_id=lead.get("id"),
                )
            except Exception as exc2:
                print(f"[researcher] BS4+LLM failed for {page['url']} ({exc2}); raw fallback for this page.")
                _record_event(
                    agent="researcher",
                    kind="page_fallback",
                    summary=f"LLM failed on {page['url']} ({exc2}); using raw fallback",
                    detail={"error": str(exc2)},
                    lead_id=lead.get("id"),
                )
                summary = page["meta"] or page["title"]
                evidence = (
                    [
                        {
                            "type": "summary",
                            "text": summary,
                            "url": page["url"],
                            "quote": page["meta"],
                            "page_title": page.get("title", ""),
                        }
                    ]
                    if summary
                    else []
                )
                page_results.append((page["url"], {"summary": summary, "evidence": evidence}))

        findings = _finalize(url, pages, page_results, emails_by_url, extractor)
        _record_event(
            agent="researcher",
            kind="buying_signals",
            summary=f"Deterministic buying-signal scan: {len(findings.get('buying_signals', []))} matches",
            detail={"count": len(findings.get("buying_signals", [])), "buying_signals": findings.get("buying_signals", [])[:8]},
            lead_id=lead.get("id"),
        )
    except Exception as exc:
        # Tier 3: plain BS4 for every page (no LLM) - the always-works safety net.
        print(f"[researcher] LLM path failed for {url} ({exc}); using raw BS4 fallback.")
        _record_event(
            agent="researcher",
            kind="tier3_fallback",
            summary=f"Whole-pipeline LLM path failed ({exc}); using raw BS4 fallback",
            detail={"error": str(exc)},
            lead_id=lead.get("id"),
        )
        try:
            pages = _crawl_pages(url)
        except Exception as exc3:
            db.update_lead(lead["id"], status=db.ERROR)
            raise RuntimeError(f"Research failed for {url}: {exc3}") from exc3
        emails_by_url = [
            (p["url"], email)
            for p in pages
            for email in _scan_page_emails(p["text"])
        ]
        page_results = []
        for page in pages:
            summary = page["meta"] or page["title"]
            evidence = (
                [
                    {
                        "type": "summary",
                        "text": summary,
                        "url": page["url"],
                        "quote": page["meta"],
                        "page_title": page.get("title", ""),
                    }
                ]
                if summary
                else []
            )
            page_results.append((page["url"], {"summary": summary, "evidence": evidence}))
        findings = _finalize(url, pages, page_results, emails_by_url, "bs4-fallback")

    # Quality gate: status=researched must mean REAL content exists.
    homepage_title = pages[0].get("title", "") if pages else ""
    findings["evidence"].extend(_stack_signal_evidence(lead, url, homepage_title))
    signal_count = sum(
        1 for e in findings["evidence"] if e["type"] in ("pain_point", "product_mention", "summary")
    )
    if signal_count == 0 and not findings["contact_emails"] and len(findings["summary"]) < 60:
        _record_event(
            agent="researcher",
            kind="quality_gate_fail",
            summary=f"Quality gate: no usable content for {url}",
            detail={"url": url, "signal_count": signal_count, "email_count": len(findings.get("contact_emails", []))},
            lead_id=lead.get("id"),
        )
        db.update_lead(lead["id"], scraped_data=findings, status=db.ERROR)
        raise RuntimeError(f"quality gate: no usable content found for {url}")

    updated = db.update_lead(
        lead["id"],
        scraped_data=findings,
        contact_email=(findings["contact_emails"] or [None])[0],
        status=db.RESEARCHED,
    )
    _record_event(
        agent="researcher",
        kind="done",
        summary=f"{lead.get('company_name')} -> researched ({len(findings['evidence'])} evidence rows)",
        detail={"lead_id": lead.get("id"), "evidence_count": len(findings["evidence"]), "buying_signals": len(findings.get("buying_signals", [])), "emails": findings.get("contact_emails", [])[:5]},
        lead_id=lead.get("id"),
    )
    return updated


def research(status: str = db.RAW, limit: int = 20) -> list[dict[str, Any]]:
    """Process all leads in `status` (default: raw)."""
    leads = db.get_leads_by_status(status, limit=limit)
    print(f"[researcher] Processing {len(leads)} lead(s) with status={status}")
    from webapp import _record_event
    _record_event(
        agent="researcher",
        kind="batch_start",
        summary=f"Researcher batch: {len(leads)} lead(s) with status={status}",
        detail={"count": len(leads), "status": status},
    )
    done: list[dict[str, Any]] = []
    for lead in leads:
        try:
            updated = research_one(lead)
            done.append(updated)
            print(f"[researcher] {updated.get('company_name')} -> researched")
        except Exception as exc:
            print(f"[researcher] ERROR {lead.get('company_name')}: {exc}")
            from webapp import _record_event
            _record_event(
                agent="researcher",
                kind="error",
                summary=f"ERROR {lead.get('company_name')}: {exc}",
                detail={"company": lead.get("company_name"), "error": str(exc)},
                lead_id=lead.get("id"),
            )
    return done


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deep-research raw leads")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--status", default=db.RAW)
    args = parser.parse_args()
    research(status=args.status, limit=args.limit)
