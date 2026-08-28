"""pipeline.py - Intent-First Lead Pipeline (replaces the old company-fit pipeline)

Run:
  python app.py hunt --target "Cloud Migration Services" --limit 20

Pipeline:
  1. Intent Hunter       - finds people/companies ASKING for the service
  2. Domain Resolver     - ensures every signal has a company website
  3. Decision Maker Find - finds the actual person to email at each company
  4. Intent Scorer       - ranks signals by recency + intent strength + DM
  5. Outreach Drafter    - generates a personalized email referencing the
                           original intent signal (the post they made)

Output: 'leads' rows in Supabase with the human's name, title, email,
LinkedIn, AND the source URL of the post that proves they're a real lead.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict
from typing import Any

from dotenv import load_dotenv

import database as db
from decision_maker import find_decision_makers, DecisionMaker
from intent_hunter import (
    IntentSignal,
    enrich_signal,
    hunt_intent,
)
from llm import chat, load_prompt, parse_json
from search import resolve_official_website

load_dotenv()


# ─── Domain resolution for intent signals ────────────────────────────────────

def _normalize_product(raw: str) -> str:
    """Extract a clean product keyword from any input (user query, scraper
    query, target_product). Strips site: / quotes / search operators so
    every lead with the same intent gets grouped under one product name.

    Output is title-cased (each word capitalized) so 'cloud migration
    services' and 'Cloud Migration Services' collapse to the same group.

    Examples:
      'site:linkedin.com/posts "need a" "Cloud Migration Services"'
          -> 'Cloud Migration Services'
      '"Cloud Migration Services engineer" hiring'
          -> 'Cloud Migration Services'
      'looking for cloud migration cloud migration services'
          -> 'Cloud Migration Services'
    """
    if not raw:
        return ""
    import re as _re
    # Words that are *suffix modifiers* — drop them only when they trail the
    # quoted phrase, never from inside a multi-word product like "Cloud
    # Migration Services". Match at the END of the string with optional
    # whitespace before.
    suffix_pattern = _re.compile(
        r"\s+(?:engineer|architect|specialist|consultant|consulting|company|companies|provider|help|partner|manager|lead|hire|hiring|job|jobs|career|careers|role|roles)\s*$",
        _re.IGNORECASE,
    )
    # 1. Pull out the last quoted phrase — that's almost always the product
    quoted = _re.findall(r'"([^"]+)"', raw)
    if quoted:
        candidate = quoted[-1]
        # Strip regex `.*` wildcards from inside the quoted phrase too
        candidate = _re.sub(r"\.\*", " ", candidate)
        # Strip leading filler words inside the quoted phrase too
        candidate = _leading_filler_re.sub("", candidate).strip()
        # Drop trailing role/job filler only (e.g. "Cloud Migration engineer" -> "Cloud Migration")
        candidate = suffix_pattern.sub("", candidate).strip()
        # Collapse internal whitespace
        candidate = _re.sub(r"\s+", " ", candidate).strip()
        if candidate:
            return _title_case(_dedupe_words(candidate))
    # 2. Fallback: strip site: / inurl: / intitle: / Boolean operators
    cleaned = _re.sub(r"site:\S+", "", raw, flags=_re.IGNORECASE)
    cleaned = _re.sub(r"\b(?:inurl:|intitle:|filetype:|inanchor:)\S*", "", cleaned, flags=_re.IGNORECASE)
    cleaned = _re.sub(r"\b(?:OR|AND)\b", " ", cleaned, flags=_re.IGNORECASE)
    cleaned = _re.sub(r"\.\*?", " ", cleaned)  # regex `.*` or `?` wildcards
    cleaned = _re.sub(r"\*", " ", cleaned)  # any other stray `*`
    cleaned = _re.sub(r"\"", "", cleaned)
    # Strip leading filler like "looking for", "best", "recommend me a"
    cleaned = _leading_filler_re.sub("", cleaned).strip()
    # Drop trailing suffix filler
    cleaned = suffix_pattern.sub("", cleaned).strip()
    cleaned = _re.sub(r"\s+", " ", cleaned).strip()
    return _title_case(_dedupe_words(cleaned)) or raw


def _dedupe_words(s: str) -> str:
    """Collapse consecutive duplicate words from a query string.
    e.g. 'cloud migration cloud migration services' -> 'cloud migration services'.
    Case-insensitive (the first occurrence wins)."""
    if not s:
        return s
    out: list[str] = []
    seen_lower: set[str] = set()
    for tok in s.split():
        if tok.lower() in seen_lower:
            continue
        seen_lower.add(tok.lower())
        out.append(tok)
    return " ".join(out)


# Words that stay lowercase in title-cased product names. Keeps acronyms like
# "AWS", "CI/CD", "GCP" intact and avoids Title-Casing prepositions when they
# trail a name.
_TITLE_STOPWORDS = {"and", "or", "of", "the", "for", "to", "in", "on", "at"}


def _title_case(s: str) -> str:
    """Title-case a product name, preserving acronyms (AWS, GCP, CI/CD, etc.)."""
    if not s:
        return s
    out: list[str] = []
    for i, tok in enumerate(s.split()):
        if not tok:
            continue
        # 1. Keep all-caps acronyms and mixed-case tokens (e.g. "vSphere", "GitHub") as-is
        if tok.isupper() or any(c.isupper() for c in tok[1:]):
            out.append(tok)
            continue
        # 2. Short lowercase tokens that are well-known acronyms (length <= 4,
        #    mostly consonants like "aws", "gcp", "sap", "api") get uppercased.
        if tok.islower() and len(tok) <= 4 and tok.lower() in {
            "aws", "gcp", "sap", "api", "crm", "erp", "db", "vm", "cdn", "sso",
            "ml", "ai", "qa", "ci", "cd", "iot", "sre", "bi", "k8s",
        }:
            out.append(tok.upper())
            continue
        # 3. Trailing stopwords stay lowercase ("Engineer for Hire" -> "Engineer for Hire")
        if i > 0 and tok.lower() in _TITLE_STOPWORDS:
            out.append(tok.lower())
            continue
        # 4. Default: title-case
        out.append(tok[:1].upper() + tok[1:].lower())
    return " ".join(out)


# Pre-compile the leading-filler regex once at module load.
import re as _re_module
# Each filler word must be followed by whitespace or end-of-string — otherwise
# the inner `\s+` groups in alternatives like "recommend me a" can backtrack
# across the next word's leading space and eat the first character of the
# actual product (e.g. turning "AWS Migration" into "WS Migration").
_leading_filler_re = _re_module.compile(
    r"^\s*(?:looking\s+for(?=\s|$)|need(?=\s|$)|best(?=\s|$)|"
    r"recommend(?:\s+me)?(?:\s+a)?(?=\s|$)|top(?=\s|$)|"
    r"hire(?=\s|$)|hiring(?=\s|$)|find(?=\s|$)|search(?:ing)?\s+for(?=\s|$)|"
    r"who\s+can(?=\s|$)|can\s+anyone(?=\s|$)|anyone\s+know(?=\s|$)|"
    r"we\s+need(?=\s|$)|i\s+need(?=\s|$)|get\s+me(?=\s|$)|"
    r"need\s+help\s+with(?=\s|$)|help\s+with(?=\s|$)|help\s+migrating(?=\s|$)|help\s+me(?=\s|$)|"
    r"site:\S+\s+)*",
    _re_module.IGNORECASE,
)


def _ensure_domain(sig: IntentSignal) -> str:
    """Make sure a signal has a usable company domain."""
    # 1. Already set from enrichment
    if sig.enriched_domain:
        return sig.enriched_domain.lower()
    # 2. Already set from TheirStack
    if sig.company_domain:
        return sig.company_domain.lower()
    # 3. Try to extract domain from source URL
    if "://" in sig.source_url:
        from urllib.parse import urlparse
        host = urlparse(sig.source_url).netloc.lower()
        # If it's a Reddit/HN/LinkedIn URL, we need to find the company
        skip_hosts = ("reddit.com", "news.ycombinator.com", "linkedin.com", "twitter.com", "x.com")
        if host and not any(s in host for s in skip_hosts):
            return host.replace("www.", "")
    # 4. Try to resolve from company name
    if sig.company_from_author or sig.enriched_company:
        name = sig.enriched_company or sig.company_from_author
        return (resolve_official_website(name) or "").lower()
    return ""


# ─── Intent scoring ──────────────────────────────────────────────────────────

def score_intent_signal(sig: IntentSignal) -> int:
    """Convert a raw intent signal into a 1-100 fit score.

    Scoring weights (intent-first):
      - Has explicit buying request:     +40
      - Recent (last 7 days):            +25
      - Recent (last 30 days):           +15
      - Has decision-maker name:         +15
      - Has company domain:              +10
      - Source: Reddit/HN:               +5  (real people, real needs)
      - Source: Job posting:             +10 (active budget)
      - Source: LinkedIn post:           +5
      - High upvotes/comments:           up to +10
    """
    score = 30  # base: any lead is a 30
    if sig.intent_confidence >= 0.8:
        score += 40
    elif sig.intent_confidence >= 0.6:
        score += 25
    elif sig.intent_confidence >= 0.4:
        score += 10

    # Recency
    if sig.posted_at:
        try:
            from datetime import datetime, timezone
            post_dt = datetime.fromisoformat(sig.posted_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_ago = (now - post_dt).days
            if days_ago <= 7:
                score += 25
            elif days_ago <= 30:
                score += 15
            elif days_ago <= 90:
                score += 5
        except Exception:
            pass

    if sig.enriched_decision_maker_name:
        score += 15
    if sig.company_domain or sig.enriched_domain:
        score += 10

    if sig.source == "job":
        score += 10
    elif sig.source in ("reddit", "hn"):
        score += 5
    elif sig.source == "linkedin":
        score += 5

    if sig.score >= 10:
        score += min(10, sig.score // 5)

    return min(100, max(1, score))


# ─── Lead saving ─────────────────────────────────────────────────────────────

def save_lead_from_signal(
    sig: IntentSignal,
    decision_makers: list[DecisionMaker],
    target_product: str = "",
) -> dict[str, Any] | None:
    """Save an intent signal + its decision-makers as a lead row in Supabase.

    Schema (matches existing leads table):
      company_name, website, target_product, tech_stack, contact_email,
      fit_score, confidence_score, reasoning, status='pending_review',
      source, scraped_data (jsonb), company_profile (jsonb),
      buyer_persona, matches_persona

    We pack the intent signal + decision-makers into scraped_data so the
    UI can show the human everything we know about this lead.
    """
    domain = sig.company_domain or sig.enriched_domain
    company_name = sig.enriched_company or sig.company_from_author or sig.author_name
    if not company_name or not domain:
        return None

    website = f"https://{domain}"
    if db.find_lead_by_website(website):
        return None  # duplicate

    # Best decision-maker's email
    primary_dm = decision_makers[0] if decision_makers else None
    contact_email = primary_dm.verified_email if primary_dm and primary_dm.verified_email else ""

    fit_score = score_intent_signal(sig)

    # Build a confidence score
    conf = 0
    if sig.intent_confidence >= 0.7: conf += 40
    elif sig.intent_confidence >= 0.5: conf += 25
    if domain: conf += 15
    if primary_dm and primary_dm.verified_email: conf += 25
    elif primary_dm: conf += 10
    if sig.posted_at: conf += 10
    if sig.source in ("reddit", "hn", "job"): conf += 10

    # Build the company_profile for the UI
    profile = {
        "source": sig.source,
        "source_url": sig.source_url,
        "source_name": sig.source_name,
        "query_used": sig.query_used,
        "post_title": sig.post_title,
        "post_snippet": sig.post_snippet,
        "posted_at": sig.posted_at,
        "intent_confidence": sig.intent_confidence,
        "decision_makers": [
            {
                "name": dm.name,
                "title": dm.title,
                "email": dm.verified_email,
                "email_confidence": dm.email_confidence,
                "linkedin_url": dm.linkedin_url,
                "source_url": dm.source_url,
                "title_match_score": dm.title_match_score,
            }
            for dm in decision_makers
        ],
    }

    # Build reasoning
    reasoning_parts = []
    if sig.post_title:
        reasoning_parts.append(f"Intent signal: '{sig.post_title}'")
    if sig.post_snippet:
        reasoning_parts.append(f"From: {sig.post_snippet[:150]}")
    if primary_dm:
        reasoning_parts.append(
            f"Decision-maker: {primary_dm.name}, {primary_dm.title} ({primary_dm.verified_email or 'no email'})"
        )
    if sig.notes:
        reasoning_parts.append(f"Notes: {sig.notes}")
    reasoning = " | ".join(reasoning_parts)[:1000]

    # tech_stack from TheirStack payload (for theirstack signals)
    tech_stack: list[str] = []

    lead = db.insert_lead(
        company_name=company_name,
        website=website,
        tech_stack=tech_stack,
        source=f"intent_{sig.source}",
        target_product=_normalize_product(target_product or sig.query_used or company_name),
        contact_email=contact_email or None,
        company_profile=profile,
    )
    # insert_lead only accepts a fixed arg set; the scoring + reasoning columns
    # are set via update_lead immediately after, which knows the full column list.
    db.update_lead(
        lead["id"],
        fit_score=fit_score,
        confidence_score=conf,
        reasoning=reasoning,
        status=db.PENDING_REVIEW,
    )
    # Re-read so the returned dict reflects the updated columns
    return db.get_lead(lead["id"])


# ─── Outreach drafting ───────────────────────────────────────────────────────

def draft_outreach_for_lead(lead: dict[str, Any]) -> dict[str, str]:
    """Generate a personalized cold email for a lead that has intent data.

    The KEY differentiator: the email references the actual post/comment
    the lead made, so it's not generic spam. e.g. 'Saw your Reddit post
    in r/sysadmin about migrating from VMware — happy to share how we
    helped [similar co] cut their migration time in half.'
    """
    profile = lead.get("company_profile") or {}
    sig_url = profile.get("source_url", "")
    sig_source = profile.get("source_name", "")
    post_title = profile.get("post_title", "")
    post_snippet = profile.get("post_snippet", "")
    dms = profile.get("decision_makers", [])
    primary = dms[0] if dms else {}
    company = lead.get("company_name", "")
    target_product = lead.get("target_product", "")

    prompt = load_prompt("outreach_intent.md")
    user_msg = f"""COMPANY: {company}
DECISION MAKER: {primary.get('name', '')} ({primary.get('title', '')})
TARGET SERVICE: {target_product}
INTENT SIGNAL SOURCE: {sig_source}
POST TITLE: {post_title}
POST SNIPPET: {post_snippet}
SOURCE URL: {sig_url}
SENDER PRODUCT: Cloud migration and infrastructure modernization services.
SENDER NAME: Shivam at Conftech
"""
    try:
        reply = chat(prompt, user_msg, max_tokens=600)
        data = parse_json(reply)
        return {
            "subject": data.get("subject", f"Re: {post_title or 'your recent post'}"),
            "email_body": data.get("body", ""),
        }
    except Exception as exc:
        print(f"[pipeline] outreach draft failed: {exc}")
        return {
            "subject": f"Re: {post_title or 'your post'}",
            "email_body": f"Hi {primary.get('name', 'there')},\n\nSaw your post on {sig_source} about {post_title[:60]}.\n\nWould love to share how we've helped similar companies.\n\nBest,\nShivam",
        }


# ─── Main pipeline ──────────────────────────────────────────────────────────

def run_intent_pipeline(
    target_product: str,
    buyer_persona: str = "",
    limit: int = 20,
    verify_emails: bool = False,
) -> dict[str, Any]:
    """Full intent-first pipeline.

    Returns:
        {
            "signals_found": N,
            "signals_enriched": N,
            "decision_makers_found": N,
            "leads_saved": N,
            "leads": [list of lead dicts],
        }
    """
    print(f"\n[pipeline] Target: {target_product!r}")
    if buyer_persona:
        print(f"[pipeline] Persona: {buyer_persona!r}")

    # 1. HUNT intent signals from all sources
    print("\n[1/5] Hunting intent signals...")
    signals = hunt_intent(target_product, buyer_persona, limit_per_source=limit)
    if not signals:
        return {"signals_found": 0, "leads_saved": 0, "leads": []}

    # 2. ENRICH each signal with LLM
    print(f"\n[2/5] Enriching {len(signals)} signals with LLM...")
    enriched = []
    for sig in signals:
        enriched_sig = enrich_signal(sig)
        domain = _ensure_domain(enriched_sig)
        if domain:
            enriched_sig.company_domain = domain
        enriched.append(enriched_sig)

    # 3. FIND decision-makers for each unique company
    print(f"\n[3/5] Finding decision-makers...")
    by_company: dict[str, list[IntentSignal]] = {}
    for sig in enriched:
        company = (sig.enriched_company or sig.company_from_author or "").strip()
        domain = sig.company_domain or sig.enriched_domain
        if not company and domain:
            company = domain.split(".")[0]
        if not company:
            continue
        key = company.lower()
        by_company.setdefault(key, []).append(sig)

    all_decision_makers: dict[str, list[DecisionMaker]] = {}
    for company, sigs in by_company.items():
        domain = sigs[0].company_domain or sigs[0].enriched_domain or ""
        dms = find_decision_makers(company, domain, max_results=2, verify_emails=verify_emails)
        if dms:
            all_decision_makers[company.lower()] = dms
            print(f"  {company}: {len(dms)} DM(s) found — {dms[0].name} ({dms[0].title})")
        else:
            print(f"  {company}: no DM found")

    # 4. SCORE & SAVE leads
    print(f"\n[4/5] Saving leads to database...")
    saved_leads: list[dict] = []
    for sig in enriched:
        company = (sig.enriched_company or sig.company_from_author or "").strip()
        if not company:
            continue
        dms = all_decision_makers.get(company.lower(), [])
        lead = save_lead_from_signal(sig, dms, target_product=target_product)
        if lead:
            saved_leads.append(lead)
            print(f"  ✓ {company} — fit={lead.get('fit_score')} — {dms[0].name if dms else 'no DM'}")
            # Auto-draft the outreach email so the reviewer sees a complete
            # lead in the UI. Failure here is non-fatal — the lead is still
            # saved as pending_review and can be approved later to retry.
            try:
                draft = draft_outreach_for_lead(lead)
                if draft.get("email_body"):
                    db.insert_draft(
                        lead["id"],
                        draft.get("subject", ""),
                        draft["email_body"],
                    )
                    print(f"    📧 draft ready: {draft.get('subject', '')[:60]}")
            except Exception as exc:
                print(f"    (draft failed: {exc})")

    print(f"\n[5/5] Done. {len(saved_leads)} leads saved (status=pending_review).")
    return {
        "signals_found": len(signals),
        "signals_enriched": len(enriched),
        "decision_makers_found": sum(len(dms) for dms in all_decision_makers.values()),
        "leads_saved": len(saved_leads),
        "leads": saved_leads,
    }


# ─── CLI entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Intent-first lead pipeline")
    parser.add_argument("--target", required=True)
    parser.add_argument("--persona", default="")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--verify-emails", action="store_true")
    args = parser.parse_args()

    db.init_schema()
    result = run_intent_pipeline(
        args.target, buyer_persona=args.persona,
        limit=args.limit, verify_emails=args.verify_emails,
    )
    print(f"\nFinal: {result['leads_saved']} leads ready for review.")
