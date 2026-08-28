"""intent_hunter.py - Intent-First Lead Discovery

Finds ACTUAL buyers by scraping platforms where people ASK for help, request
recommendations, or post job listings indicating they need a service.

Free data sources (no API keys needed):
  1. Reddit RSS feeds  - r/sysadmin, r/aws, r/msp, r/devops, r/cloud,
                          r/smallbusiness, r/entrepreneur, r/startups
  2. HN Algolia API   - Ask HN posts + Show HN with buyer signals
  3. DuckDuckGo       - job boards, LinkedIn posts, "looking for" queries

Each "hit" is a raw intent signal. The caller (orchestrated in pipeline.py)
enriches it with company data + decision-maker contact info.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import requests
from dotenv import load_dotenv

import cache
import search as search_lib
from llm import chat, load_prompt, parse_json

load_dotenv()

# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class IntentSignal:
    """A person/company that has expressed a need for the target service."""
    source: str                       # 'reddit' | 'hn' | 'linkedin' | 'job' | 'ddg'
    source_url: str                   # Direct link to the post/comment
    source_name: str                  # Display name of source (subreddit, HN, etc.)
    query_used: str                  # The search query that found this
    author_name: str                 # Poster's username
    author_url: str = ""             # Poster's profile URL (Reddit, HN, etc.)
    company_from_author: str = ""     # Company name extracted from author bio/post
    company_domain: str = ""         # Resolved company website
    post_title: str = ""
    post_snippet: str = ""           # Relevant excerpt from the post
    post_body: str = ""             # Full post text (if available)
    posted_at: str = ""             # ISO timestamp when posted
    score: int = 0                  # Upvotes / karma on the post
    comment_count: int = 0
    # LLM-extracted enrichment (done in 2nd pass)
    enriched_company: str = ""
    enriched_domain: str = ""
    enriched_decision_maker_name: str = ""
    enriched_decision_maker_title: str = ""
    enriched_decision_maker_email: str = ""
    enriched_decision_maker_linkedin: str = ""
    intent_confidence: float = 0.0   # 0-1, how strong a buying signal
    fit_score: int = 0              # 1-100 overall lead quality
    notes: str = ""


# ─── Reddit RSS ───────────────────────────────────────────────────────────────

_REDDIT_SUBREDDITS = [
    # Ops / IT / infra communities — people actually dealing with migration pain
    "sysadmin", "aws", "azure", "googlecloud", "devops", "MsAzure",
    "cloud", "infrastructure", "datacenter", "networking", "linuxadmin",
    # Business communities — decision makers asking about services
    "smallbusiness", "entrepreneur", "startups", "busines", "marketing",
    # Vertical communities where companies ASK for help publicly
    "manufacturing", "healthcare", "fintech", "logistics", "ecommerce",
    "retail", "distribution", "supplychain",
    # MSP / consulting buyers — companies looking for vendors
    "msp", "mspSales",
]

# Search query templates per service type
_SERVICE_QUERY_TEMPLATES = {
    "migration": [
        "help migrating to cloud", "looking for cloud migration",
        "need aws migration help", "migrating from on-prem",
        "legacy system migration help", "lift and shift",
        "cloud migration partner", "migrating to azure",
        "we need help with cloud migration", "evaluating cloud migration",
    ],
    "devops": [
        "looking for devops help", "need devops engineer",
        "ci cd pipeline help", "kubernetes migration",
        "need site reliability help", "devops consultant",
    ],
    "security": [
        "need security audit", "looking for cybersecurity help",
        "soc 2 compliance help", "penetration testing service",
        "need security assessment",
    ],
    "data": [
        "data warehouse help", "looking for data engineer",
        "analytics platform migration", "bi tool help",
    ],
    "default": [
        "looking for {service} help", "need {service} provider",
        "{service} consultant recommendation", "best {service} company",
    ],
}

# Subreddits that are COMPETITORS (service providers talking to each other) — exclude
_REDDIT_EXCLUDE_SUBREDDITS = {"msp", "mspsales", "mspSales"}


def _build_reddit_queries(target_product: str) -> list[tuple[str, str]]:
    """Build (subreddit, search_query) tuples for Reddit RSS."""
    queries = []
    low = target_product.lower()
    service_type = "default"
    for key in _SERVICE_QUERY_TEMPLATES:
        if key != "default" and key in low:
            service_type = key
            break
    templates = _SERVICE_QUERY_TEMPLATES.get(service_type, _SERVICE_QUERY_TEMPLATES["default"])
    # Replace {service} placeholder
    templates = [t.replace("{service}", target_product.lower()) for t in templates]

    for sub in _REDDIT_SUBREDDITS:
        if sub.lower() in _REDDIT_EXCLUDE_SUBREDDITS:
            continue
        for q in templates[:2]:  # 2 queries per sub max
            queries.append((sub, q))
    return queries


def _parse_reddit_entry(entry_xml: str, subreddit: str, query: str) -> IntentSignal | None:
    """Parse a Reddit RSS <entry> element into an IntentSignal."""
    try:
        root = ET.fromstring(entry_xml)
        ns = {"atom": "http://www.w3.org/2005/Atom", "reddit": "https://www.reddit.com/terms/"}
        # Reddit RSS uses <author><name> for username, <title> for post title
        author_el = root.find("author/name")
        title_el = root.find("title")
        link_el = root.find("link[@rel='alternate']")
        id_el = root.find("id")
        updated_el = root.find("updated")
        content_el = root.find("content")

        author = author_el.text.strip() if author_el is not None and author_el.text else ""
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        url = ""
        if link_el is not None:
            url = link_el.get("href", "")
        post_id = id_el.text.strip() if id_el is not None and id_el.text else ""
        updated = updated_el.text.strip() if updated_el is not None and updated_el.text else ""
        content = content_el.text.strip() if content_el is not None and content_el.text else ""
        # Reddit RSS content is HTML
        body = re.sub(r"<[^>]+>", " ", content or "")
        body = re.sub(r"\s+", " ", body).strip()

        if not title or not url:
            return None

        return IntentSignal(
            source="reddit",
            source_url=url,
            source_name=f"r/{subreddit}",
            query_used=query,
            author_name=author,
            author_url=f"https://www.reddit.com/user/{author}" if author else "",
            post_title=title,
            post_snippet=body[:300] if body else "",
            post_body=body,
            posted_at=updated,
            score=0,
        )
    except Exception:
        return None


def scrape_reddit(target_product: str, limit_per_sub: int = 3) -> list[IntentSignal]:
    """Scrape Reddit RSS for buyer-intent posts about the target product."""
    signals: list[IntentSignal] = []
    queries = _build_reddit_queries(target_product)
    seen_urls: set[str] = set()

    for subreddit, query in queries:
        if len(signals) >= limit_per_sub * len(_REDDIT_SUBREDDITS):
            break

        rss_url = f"https://www.reddit.com/r/{subreddit}/search.rss?q={requests.utils.quote(query)}&sort=new&restrict_sr=on&t=month&limit={limit_per_sub}"
        try:
            r = requests.get(
                rss_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; LeadBot/1.0)"},
                timeout=15,
            )
            if r.status_code == 429:
                print(f"[intent_hunter] Reddit 429 for r/{subreddit} — backing off 5s")
                time.sleep(5.0)
                continue
            if r.status_code != 200:
                continue
            # Parse the feed — entries are <entry> elements
            text = r.text
            # Extract each <entry>...</entry> block
            entries = re.findall(r"<entry>(.*?)</entry>", text, re.DOTALL)
            for entry_xml in entries:
                sig = _parse_reddit_entry(f"<entry>{entry_xml}</entry>", subreddit, query)
                if sig and sig.source_url not in seen_urls:
                    seen_urls.add(sig.source_url)
                    signals.append(sig)
        except Exception as exc:
            print(f"[intent_hunter] Reddit RSS failed for r/{subreddit} ({query}): {exc}")
            continue
        # Reddit's anonymous RSS endpoint rate-limits aggressively. 1.2s
        # between requests keeps us well under the 60-req/min ceiling when
        # we hit 20+ subreddits.
        time.sleep(1.2)

    print(f"[intent_hunter] Reddit: found {len(signals)} intent signals")
    return signals


# ─── Hacker News ─────────────────────────────────────────────────────────────

_HN_QUERY_TEMPLATES = [
    "looking for cloud migration",
    "need help with cloud migration",
    "ask hn cloud migration service",
    "recommend cloud migration consultant",
    "vendor migration help",
    "migrating from on-prem",
    "ask hn devops help",
    "need aws migration partner",
    "looking for devops consultant",
]


def scrape_hackernews(target_product: str, limit: int = 10) -> list[IntentSignal]:
    """Scrape HN Algolia for Ask HN / Show HN posts with buyer intent."""
    signals: list[IntentSignal] = []
    seen_urls: set[str] = set()

    for template in _HN_QUERY_TEMPLATES[:6]:
        query = f"{template} {target_product.lower()}"
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": query, "tags": "ask_hn,story", "hitsPerPage": limit, "numericFilters": "points>1"},
                timeout=15,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            for hit in data.get("hits", []):
                url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                author = hit.get("author", "")
                sig = IntentSignal(
                    source="hn",
                    source_url=url,
                    source_name="Hacker News",
                    query_used=query,
                    author_name=author,
                    author_url=f"https://news.ycombinator.com/user?id={author}" if author else "",
                    post_title=hit.get("title", ""),
                    post_snippet=hit.get("excerpt", "")[:300],
                    posted_at=hit.get("created_at", ""),
                    score=hit.get("points", 0),
                    comment_count=hit.get("num_comments", 0),
                )
                signals.append(sig)
        except Exception as exc:
            print(f"[intent_hunter] HN Algolia failed for '{query}': {exc}")
            continue

    print(f"[intent_hunter] HN: found {len(signals)} intent signals")
    return signals


# ─── LinkedIn Posts via DDG ──────────────────────────────────────────────────

def scrape_linkedin_posts(target_product: str, limit: int = 10) -> list[IntentSignal]:
    """Search DDG for LinkedIn posts where people are asking for the service."""
    signals: list[IntentSignal] = []
    seen_urls: set[str] = set()

    queries = [
        f'site:linkedin.com/posts "looking for" "{target_product}"',
        f'site:linkedin.com/posts "need a" "{target_product}"',
        f'site:linkedin.com/posts "evaluating" "{target_product}"',
        f'site:linkedin.com/posts "recommend" "{target_product}"',
    ]

    for q in queries:
        if len(signals) >= limit:
            break
        try:
            sr = search_lib.web_search(q, max_results=10)
            for item in sr.get("organic", []):
                link = item.get("link", "")
                if "linkedin.com/posts/" not in link and "linkedin.com/feed/update/" not in link:
                    continue
                if link in seen_urls:
                    continue
                seen_urls.add(link)
                # Extract author from snippet if possible
                snippet = item.get("snippet", "")
                sig = IntentSignal(
                    source="linkedin",
                    source_url=link,
                    source_name="LinkedIn",
                    query_used=q,
                    author_name="",
                    post_title=item.get("title", ""),
                    post_snippet=snippet[:300],
                    score=0,
                )
                signals.append(sig)
        except Exception as exc:
            print(f"[intent_hunter] LinkedIn DDG search failed for '{q}': {exc}")
            continue

    print(f"[intent_hunter] LinkedIn: found {len(signals)} intent signals")
    return signals


# ─── Job Postings (Indeed / LinkedIn Jobs via DDG) ─────────────────────────────

def scrape_job_postings(target_product: str, limit: int = 10) -> list[IntentSignal]:
    """Find companies actively HIRING for roles that imply they need the service.

    A company posting for 'Cloud Migration Engineer' or 'AWS Architect' is
    actively buying cloud migration services — the person they hire IS the service.
    This is one of the strongest intent signals.
    """
    signals: list[IntentSignal] = []
    seen_urls: set[str] = set()

    # Role patterns that indicate active buying of the service
    migration_role_queries = [
        f'"{target_product} engineer" hiring',
        f'"{target_product} architect" hiring',
        f'cloud migration specialist job',
        f'aws migration engineer job',
        f'site:linkedin.com/jobs "cloud migration"',
    ]

    for q in migration_role_queries:
        if len(signals) >= limit:
            break
        try:
            sr = search_lib.web_search(q, max_results=10)
            for item in sr.get("organic", []):
                link = item.get("link", "")
                if not link or link in seen_urls:
                    continue
                seen_urls.add(link)
                title = item.get("title", "")
                snippet = item.get("snippet", "")
                # Try to extract company name from title: "Company Name is hiring..."
                company_match = re.search(r"^(.+?)\s+(?:is hiring|jobs|careers)", title, re.IGNORECASE)
                company = company_match.group(1).strip() if company_match else ""
                sig = IntentSignal(
                    source="job",
                    source_url=link,
                    source_name="Job Posting",
                    query_used=q,
                    author_name=company,
                    post_title=title,
                    post_snippet=snippet[:300],
                    company_from_author=company,
                    score=0,
                )
                signals.append(sig)
        except Exception as exc:
            print(f"[intent_hunter] Job search failed for '{q}': {exc}")
            continue

    print(f"[intent_hunter] Job postings: found {len(signals)} intent signals")
    return signals


# ─── Reddit "recommend me a X" posts ─────────────────────────────────────────

def scrape_recommendation_posts(target_product: str, limit: int = 8) -> list[IntentSignal]:
    """Find 'recommend me a X provider' / 'which X company should I use' posts.

    These are WARM leads — the person has budget, has identified the need,
    and is in the evaluation phase. Highest conversion potential.
    """
    signals: list[IntentSignal] = []
    seen_urls: set[str] = set()

    queries = [
        f'site:reddit.com "recommend.* {target_product}"',
        f'site:reddit.com "which.* {target_product}.*provider"',
        f'site:reddit.com "looking for.*{target_product}.*company"',
        f'site:reddit.com "best.*{target_product}.*company"',
        f'site:reddit.com "hiring.*{target_product}.*consultant"',
    ]

    for q in queries:
        if len(signals) >= limit:
            break
        try:
            sr = search_lib.web_search(q, max_results=8)
            for item in sr.get("organic", []):
                link = item.get("link", "")
                if not link or link in seen_urls:
                    continue
                seen_urls.add(link)
                sig = IntentSignal(
                    source="reddit",
                    source_url=link,
                    source_name="Reddit",
                    query_used=q,
                    author_name="",
                    post_title=item.get("title", ""),
                    post_snippet=item.get("snippet", "")[:300],
                    score=0,
                )
                signals.append(sig)
        except Exception as exc:
            print(f"[intent_hunter] Recommendation search failed for '{q}': {exc}")
            continue

    print(f"[intent_hunter] Reddit recommendations: found {len(signals)} intent signals")
    return signals


# ─── TheirStack (refocused) ──────────────────────────────────────────────────

def scrape_theirstack_lean(
    target_product: str,
    buyer_persona: str = "",
    limit: int = 8,
) -> list[IntentSignal]:
    """TheirStack, but TWEAKED: target companies on LEGACY tech that are
    actively growing (hiring, recent funding) — not just any 50-2000 emp company.

    The original pipeline searched for companies on VMware/SAP/etc, which
    surfaces enterprise giants. This version adds funding/hiring filters to
    find smaller companies that are actively evolving — more likely to be
    in the evaluation phase for modernization services.
    """
    try:
        from prospector import search_theirstack, classify_company, ENTERPRISE_EMPLOYEE_MIN
    except ImportError:
        print("[intent_hunter] TheirStack unavailable (prospector not importable)")
        return []

    # Build target-aware payload
    low = target_product.lower()
    tech_slugs: list[str] = []
    if any(k in low for k in ("migration", "moderniz", "lift and shift", "legacy")):
        tech_slugs = ["vmware-vsphere", "microsoft-hyperv", "oracle-database",
                       "sap", "windows-server", "microsoft-sql-server"]
    elif any(k in low for k in ("devops", "ci/cd", "kubernetes", "sre")):
        tech_slugs = ["jenkins", "bamboo-ci", "subversion", "perforce"]
    elif any(k in low for k in ("cybersecurity", "security", "compliance")):
        tech_slugs = ["cisco", "fortinet", "palo-alto-networks", "okta"]
    elif any(k in low for k in ("license", "cost", "sam", "spend")):
        tech_slugs = ["sap", "oracle-database", "microsoft-365", "salesforce"]
    else:
        tech_slugs = ["vmware-vsphere", "sap", "oracle-database", "windows-server"]

    # CRITICAL: target the SWEET SPOT — 20 to 2000 employees
    # Small enough to make buying decisions fast
    # Large enough to have real budget
    signals: list[IntentSignal] = []
    seen_domains: set[str] = set()

    payloads = [
        # Combined: legacy tech + company description hints (buyer signals)
        {
            "tech_filters": {"technology_slug_or": tech_slugs[:4]},
            "company_description_pattern_or": [
                "manufacturing", "logistics", "distribution", "retail",
                "healthcare", "financial", "fintech", "insurance",
            ],
            "min_employee_count_or_null": 20,
            "max_employee_count_or_null": 2000,
            "limit": 4,
        },
        # Safety net: just legacy tech + smaller size
        {
            "tech_filters": {"technology_slug_or": tech_slugs[:3]},
            "min_employee_count_or_null": 20,
            "max_employee_count_or_null": 1500,
            "limit": 4,
        },
    ]

    for payload in payloads:
        if len(signals) >= limit:
            break
        try:
            result = search_theirstack(payload)
            for co in result.get("data", []) or []:
                domain = (co.get("domain") or "").strip().lower()
                if not domain or domain in seen_domains:
                    continue
                seen_domains.add(domain)

                cls = classify_company(co)
                if cls["action"] == "exclude":
                    continue

                sig = IntentSignal(
                    source="theirstack",
                    source_url=f"https://{domain}",
                    source_name="TheirStack",
                    query_used=f"tech={tech_slugs}, target={target_product}",
                    author_name=co.get("name", ""),
                    company_from_author=co.get("name", ""),
                    company_domain=domain,
                    post_title=f"{co.get('name', '')} — {co.get('industry', 'Company')}",
                    post_snippet=co.get("seo_description", "")[:300],
                    score=cls["profile"].get("num_jobs") or 0,
                )
                signals.append(sig)
        except Exception as exc:
            print(f"[intent_hunter] TheirStack failed: {exc}")
            continue

    print(f"[intent_hunter] TheirStack lean: found {len(signals)} intent signals")
    return signals


# ─── Main dispatcher ────────────────────────────────────────────────────────────

def hunt_intent(
    target_product: str,
    buyer_persona: str = "",
    limit_per_source: int = 12,
) -> list[IntentSignal]:
    """Aggregate intent signals from ALL sources.

    This is the main entry point. Runs all sources in parallel-ish
    (sequential but fast — no external API keys needed for most).
    Deduplicates by source_url.
    """
    all_signals: list[IntentSignal] = []
    seen_urls: set[str] = set()

    sources = [
        ("reddit", lambda: scrape_reddit(target_product, limit_per_source)),
        ("hn", lambda: scrape_hackernews(target_product, limit_per_source)),
        ("linkedin", lambda: scrape_linkedin_posts(target_product, limit_per_source)),
        ("jobs", lambda: scrape_job_postings(target_product, limit_per_source)),
        ("recommend", lambda: scrape_recommendation_posts(target_product, limit_per_source)),
        ("theirstack", lambda: scrape_theirstack_lean(target_product, buyer_persona, limit_per_source)),
    ]

    for name, fn in sources:
        try:
            for sig in fn():
                if sig.source_url and sig.source_url not in seen_urls:
                    seen_urls.add(sig.source_url)
                    all_signals.append(sig)
        except Exception as exc:
            print(f"[intent_hunter] Source '{name}' raised: {exc}")

    print(f"[intent_hunter] TOTAL: {len(all_signals)} unique intent signals across all sources")
    return all_signals


# ─── LLM enrichment ──────────────────────────────────────────────────────────

def enrich_signal(sig: IntentSignal) -> IntentSignal:
    """Use LLM to extract company name, domain, decision-maker name/title
    from a raw intent signal. Falls back to deterministic parsing on failure.

    This is the second pass: we have a post/snippet, now figure out WHO
    posted it and whether they represent a real company.
    """
    prompt = load_prompt("intent_enrich.md")
    try:
        reply = chat(
            prompt,
            f"POST TITLE: {sig.post_title}\n"
            f"POST SNIPPET: {sig.post_snippet}\n"
            f"POST BODY: {sig.post_body[:1000]}\n"
            f"SOURCE: {sig.source} ({sig.source_name})\n"
            f"AUTHOR: {sig.author_name}\n"
            f"URL: {sig.source_url}",
            max_tokens=600,
        )
        data = parse_json(reply)

        sig.enriched_company = data.get("company_name", "")
        sig.enriched_domain = data.get("domain", "")
        sig.enriched_decision_maker_name = data.get("decision_maker_name", "")
        sig.enriched_decision_maker_title = data.get("decision_maker_title", "")
        sig.intent_confidence = float(data.get("intent_confidence", 0.5))
        sig.notes = data.get("notes", "")

        # If LLM found a domain, use it; otherwise try to resolve
        if sig.enriched_domain and not sig.company_domain:
            sig.company_domain = sig.enriched_domain
        if sig.enriched_company and not sig.company_from_author:
            sig.company_from_author = sig.enriched_company

        return sig
    except Exception as exc:
        print(f"[intent_hunter] LLM enrich failed for '{sig.post_title[:60]}': {exc}")
        return sig


def enrich_signals_batch(signals: list[IntentSignal], max_concurrent: int = 5) -> list[IntentSignal]:
    """Enrich all signals with LLM extraction. Returns enriched signals."""
    import concurrent.futures
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        futures = {ex.submit(enrich_signal, sig): sig for sig in signals}
        for fut in concurrent.futures.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                print(f"[intent_hunter] Enrich batch item failed: {exc}")
                results.append(futures[fut])
    return results


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Intent-first lead discovery")
    parser.add_argument("--target", required=True, help="Target service, e.g. 'Cloud Migration Services'")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    signals = hunt_intent(args.target, limit_per_source=args.limit)
    print(f"\n=== {len(signals)} signals found ===\n")
    for s in signals[:20]:
        print(f"[{s.source.upper():9s}] {s.post_title[:80]}")
        print(f"  URL: {s.source_url}")
        print(f"  Author: {s.author_name} | Company: {s.company_from_author or s.enriched_company}")
        print(f"  Snippet: {s.post_snippet[:120]}")
        print()
