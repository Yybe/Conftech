"""scorer.py - The Scorer agent.

Analyzes scraped company data vs. the Target Product using the LLM (DeepSeek
V4 Flash, fallback Gemma - via OpenRouter) and assigns fit_score (1-100) +
reasoning. Every score is backed by an evidence trail: the LLM cites specific
findings (claim + source URL + verbatim quote) drawn ONLY from the researcher's
`scraped_data["evidence"]`, and the scorer persists those citations as
`scraped_data["score_evidence"]` after validating every URL against the
provided evidence list (invented URLs are dropped). Moves leads from
'researched' to 'pending_review'.

Improvements over the base scorer:
- Deterministic `confidence_score` (0-100) derived from measurable factors
  (pages crawled, evidence volume, verified quotes, tech detections, structured
  info, cross-page consistency, extractor tier) - never an LLM guess.
- `top_reasons`: human-readable "why this lead scored high" list, strongest
  first, each backed by url/quote/page_title.
- Explicit buying signals (cloud migration, legacy system, technical debt, ...)
  carry significant positive weight in both the LLM prompt context and the
  deterministic fallback scorer.
- Poor cold-outreach targets (public sector, defense, large enterprises) get a
  deterministic score penalty from `company_profile` metadata + page signals.

Scoring rubric lives in /prompts/scorer.md.
"""

from __future__ import annotations

import json
import re
from typing import Any

import database as db
from llm import chat_json, load_prompt
from researcher import BUYING_SIGNAL_PHRASES

SCORER_MAX_TOKENS = 3000
MAX_EVIDENCE_INPUT = 20
MAX_EVIDENCE_OUTPUT = 4
MAX_STACK_ITEMS = 60

# Slugs that signal legacy/hybrid infrastructure -> prioritized for the LLM.
_LEGACY_STACK_HINTS = (
    "vmware", "hyperv", "mainframe", "windows-server", "windows-200", "windows-nt",
    "oracle", "sap", "ibm", "netapp", "mssql", "db2", "cobol", "legacy", "solaris",
    "exchange", "sharepoint", "iis",
)
# Slugs that signal modern cloud -> also relevant for migration conversations.
_CLOUD_STACK_HINTS = (
    "aws", "amazon-web-services", "azure", "google-cloud", "gcp", "kubernetes",
    "cloudflare", "terraform",
)

# buying_signal is the strongest evidence type; it must reach the LLM first.
_TYPE_PRIORITY = {
    "buying_signal": -1, "pain_point": 0, "stack_signal": 1, "product_mention": 2,
    "summary": 3, "email": 4,
}

# Per-type caps so the LLM sees a REPRESENTATIVE mix, not a flood of one type.
_PICK_CAPS = {
    "buying_signal": 6, "pain_point": 6, "stack_signal": 6,
    "product_mention": 4, "summary": 3, "email": 1,
}

# Enterprise/public-sector phrases in page text -> cold-outreach penalty.
# Segment weights come from leads.company_profile (set at prospecting time).
_SEGMENT_PENALTY = {
    "listing": -15,              # publicly traded -> enterprise-ish
    "government_adjacent": -20,  # serves the public sector; not an SMB buyer
    "large_enterprise": -35,
    "public_sector": -35,
}
_PAGE_ENTERPRISE_PHRASES = (
    "fortune 500", "fortune 100", "multinational", "global 500", "public sector",
    "government agency", "defense", "military", "publicly traded", "listed company",
)


def _pick_stack(tech_stack: list[str], cap: int = MAX_STACK_ITEMS) -> list[str]:
    """Prioritize legacy/hybrid + cloud slugs (migration signals), then fill the rest."""
    techs = list(tech_stack or [])
    def rank(t: str) -> tuple[int, int]:
        low = t.lower()
        legacy = next((i for i, k in enumerate(_LEGACY_STACK_HINTS) if k in low), 99)
        cloud = next((i for i, k in enumerate(_CLOUD_STACK_HINTS) if k in low), 99)
        return (legacy, cloud)
    picked = sorted(techs, key=rank)[:cap]
    return picked


def _pick_evidence(scraped: dict[str, Any]) -> list[dict[str, Any]]:
    """Balanced evidence mix: strongest types first, capped per type."""
    evidence = list((scraped or {}).get("evidence") or [])
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in evidence:
        groups.setdefault(e.get("type", "other"), []).append(e)

    picked: list[dict[str, Any]] = []
    for etype in sorted(_TYPE_PRIORITY, key=_TYPE_PRIORITY.get):
        picked.extend((groups.get(etype) or [])[:_PICK_CAPS.get(etype, 0)])
    return picked[:MAX_EVIDENCE_INPUT]


def _validate_evidence(
    raw_items: list[Any], allowed_urls: set[str], source: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Keep only well-formed citations whose URL exists in the provided evidence.

    Also enriches each citation with the page title + evidence type of the
    source finding it maps back to (matched by quote/text/url), so the UI can
    show where every claim came from and the reason labels stay deterministic.
    """
    by_url: dict[str, list[dict[str, Any]]] = {}
    for e in source or []:
        by_url.setdefault(str(e.get("url", "")).rstrip("/"), []).append(e)

    validated: list[dict[str, str]] = []
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim", "")).strip()
        url = str(item.get("url", "")).strip()
        quote = str(item.get("quote", "")).strip()
        if not claim or not url:
            continue
        if url.rstrip("/") not in allowed_urls:
            continue

        src: dict[str, Any] = {}
        for s in by_url.get(url.rstrip("/"), []):
            if (str(s.get("quote") or "") == quote) or (str(s.get("text") or "") == claim):
                src = s
                break
        if not src:
            src = (by_url.get(url.rstrip("/")) or [{}])[0]

        validated.append(
            {
                "claim": claim,
                "url": url,
                "quote": quote,
                "page_title": str(src.get("page_title") or ""),
                "type": str(src.get("type") or ""),
            }
        )
        if len(validated) >= MAX_EVIDENCE_OUTPUT:
            break
    return validated


# --- Deterministic confidence score ------------------------------------------

_ANALYTICAL_TYPES = ("pain_point", "product_mention", "summary", "buying_signal")


def compute_confidence(scraped: dict[str, Any], tech_stack: list) -> int:
    """Deterministic 0-100 confidence from measurable, verifiable factors.

    Components (all computed, never LLM-guessed):
      - pages crawled                          (0-16)
      - analytical evidence volume              (0-24)
      - share of evidence with a verbatim quote (0-16)
      - TheirStack technology detections        (0-10)
      - structured info depth (pain, mentions, summary, mission) (0-10)
      - cross-page consistency: how many crawled pages produced evidence (0-12)
      - explicit buying signals found           (0-8)
      - extractor tier bonus (ScrapeGraphAI > BS4+LLM > BS4 raw) (0-4)
    """
    pages = scraped.get("pages_crawled") or []
    evidence = scraped.get("evidence") or []
    analytic = [e for e in evidence if e.get("type") in _ANALYTICAL_TYPES]
    quoted = sum(1 for e in analytic if str(e.get("quote") or "").strip())
    stack = [t for t in (tech_stack or []) if isinstance(t, str) and t.strip()]
    pain = scraped.get("pain_points") or []
    mentions = scraped.get("product_mentions") or []
    buying = [e for e in evidence if e.get("type") == "buying_signal"]
    structured = sum(1 for x in (scraped.get("summary"), scraped.get("mission")) if x)
    n_pages = max(len(pages), 1)
    pages_with_evidence = min(len({e.get("url") for e in evidence if e.get("url")}), n_pages)
    extractor = str(scraped.get("extractor") or "")

    score = 0.0
    score += min(len(pages), 8) * 2.0                                              # 0-16
    score += min(len(analytic), 12) * 2.0                                          # 0-24
    score += (quoted / max(len(analytic), 1)) * 16.0                               # 0-16
    score += min(len(stack), 10) * 1.0                                             # 0-10
    score += min(len(pain), 6) * 1.0 + min(len(mentions), 4) * 0.5 + structured * 2.0  # 0-10
    score += (pages_with_evidence / n_pages) * 12.0                                # 0-12
    score += min(len(buying), 4) * 2.0                                             # 0-8
    if "scrapegraphai" in extractor:
        score += 4.0
    elif "bs4" in extractor:
        score += 2.0
    return min(100, round(score))


# --- Top reasons (human-readable "why") --------------------------------------

def _reason_label(item: dict[str, Any]) -> str:
    """Deterministic human label for one evidence/citation item."""
    text = str(item.get("claim") or item.get("text") or "")
    quote = str(item.get("quote") or "")
    low = " ".join([text, quote]).lower()

    for phrase in BUYING_SIGNAL_PHRASES:
        if phrase in low:
            return f"Buying signal — {phrase}"

    if "runs " in text:
        tech = text.split("runs ", 1)[1].strip()
        for k in _LEGACY_STACK_HINTS:
            if k in tech:
                return f"Legacy infrastructure — {tech}"
        for k in _CLOUD_STACK_HINTS:
            if k in tech:
                return f"Cloud stack — {tech}"
        return f"Tech signal — {tech}"

    etype = str(item.get("type") or "")
    if etype == "pain_point":
        return f"Pain point — {text}"
    if etype == "product_mention":
        return f"Tech mention — {text}"
    if etype == "summary":
        return f"Context — {text}"
    return text


def build_top_reasons(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn validated citations into a strongest-first 'why this lead' list.

    Each reason keeps its source url, quote, and page title so a reviewer can
    click straight to the page that produced the evidence.
    """
    reasons: list[dict[str, Any]] = []
    for item in items:
        reasons.append(
            {
                "reason": _reason_label(item),
                "claim": str(item.get("claim") or item.get("text") or ""),
                "url": str(item.get("url") or ""),
                "quote": str(item.get("quote") or ""),
                "page_title": str(item.get("page_title") or ""),
            }
        )
    return reasons[:6]


# --- Poor cold-outreach target penalty ---------------------------------------

def _filter_penalty(lead: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    """Deterministic penalty for poor cold-outreach targets.

    Sources: (1) leads.company_profile segment set by the prospector from
    public metadata; (2) enterprise/public-sector phrases found in research
    evidence. Returns (negative adjustment, filter findings with sources).
    """
    profile = lead.get("company_profile") or {}
    segment = str(profile.get("segment") or "smb_mid")
    penalty = _SEGMENT_PENALTY.get(segment, 0)
    findings: list[dict[str, Any]] = []

    if penalty:
        findings.append(
            {
                "reason": f"segment={segment} (from public company metadata)",
                "claim": (
                    f"Company profile indicates a poor cold-outreach target: "
                    f"{segment}. " + ("; ".join(profile.get("reasons") or []))
                ),
                "url": lead.get("website") or "",
                "quote": "",
                "page_title": "",
            }
        )

    # Page-level enterprise/public-sector signals (each with a real source).
    seen: set[str] = set()
    for ev in (lead.get("scraped_data") or {}).get("evidence", []):
        low = " ".join([str(ev.get("text") or ""), str(ev.get("quote") or "")]).lower()
        for phrase in _PAGE_ENTERPRISE_PHRASES:
            if phrase in low and phrase not in seen:
                seen.add(phrase)
                penalty -= 10
                findings.append(
                    {
                        "reason": f"enterprise/public-sector phrase {phrase!r} on page",
                        "claim": f"Page mentions {phrase!r}",
                        "url": str(ev.get("url") or ""),
                        "quote": str(ev.get("quote") or ""),
                        "page_title": str(ev.get("page_title") or ""),
                    }
                )
                break
        if len(seen) >= 2:
            break

    penalty = max(-50, penalty)
    return penalty, findings


# --- Deterministic fallback (LLM rate-limited / unavailable) ------------------

_SCORER_STOPWORDS = {
    "the", "and", "for", "with", "our", "your", "that", "this", "from",
    "are", "was", "were", "has", "have", "had", "not", "you", "we", "us",
    "can", "will", "would", "could", "should", "may", "might", "into",
    "over", "under", "about", "than", "then", "them", "who", "what",
    "when", "where", "which", "more", "most", "their", "its", "services",
    "solutions", "company", "platform", "software", "systems",
    "management", "product", "products",
}


def _sig_words(s: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9']+", (s or "").lower())
        if len(w) > 2 and w not in _SCORER_STOPWORDS
    }


def _kw_overlap(a: set[str], b: set[str]) -> bool:
    """True if any token matches (exact or 4+ char prefix, e.g. migration~migrations)."""
    for x in a:
        for y in b:
            if x == y or (len(x) >= 4 and len(y) >= 4 and (x.startswith(y) or y.startswith(x))):
                return True
    return False


def _rule_based_score(
    target_product: str, scraped: dict[str, Any], tech_stack: list
) -> dict[str, Any]:
    """Deterministic evidence-based scoring (used when the LLM is down).

    Every citation comes from the researched evidence list (real URLs/quotes).
    Calibration: explicit buying signals are the strongest migration signals,
    then legacy + cloud stack signals, then keyword-matched pain points.
    """
    evidence = _pick_evidence(scraped)
    tkw = _sig_words(target_product)

    legacy: set[str] = set()
    cloud: set[str] = set()
    pain_hits = 0
    sum_hits = 0
    buying_hits = 0
    for e in evidence:
        t = (e.get("text") or "").lower()
        etype = e.get("type")
        if etype == "buying_signal":
            buying_hits += 1
        elif etype == "stack_signal":
            if any(k in t for k in _LEGACY_STACK_HINTS):
                legacy.add(t)
            if any(k in t for k in _CLOUD_STACK_HINTS):
                cloud.add(t)
        elif etype in ("pain_point", "product_mention"):
            if _kw_overlap(_sig_words(e.get("text")), tkw):
                pain_hits += 1
        elif etype == "summary":
            if _kw_overlap(_sig_words(e.get("text")), tkw):
                sum_hits += 1

    n_legacy, n_cloud, n_bs = len(legacy), len(cloud), min(buying_hits, 3)
    # Explicit buying signals dominate: they guarantee conversation-relevance.
    if n_bs >= 2:
        base = 55
    elif n_bs == 1:
        base = 40
    elif n_legacy >= 2 and n_cloud >= 2:
        base = 50
    elif n_legacy >= 2 and n_cloud >= 1:
        base = 45
    elif n_legacy >= 1 or n_cloud >= 2:
        base = 35
    else:
        base = 15
    fit = min(
        100,
        base
        + 7 * n_bs
        + 3 * min(n_legacy, 6)
        + 2 * min(n_cloud, 6)
        + 4 * min(pain_hits, 3)
        + 2 * min(sum_hits, 2),
    )

    scored: list[tuple[int, dict[str, Any]]] = []
    for e in evidence:
        t = (e.get("text") or "").lower()
        etype = e.get("type")
        pts = 0
        if etype == "buying_signal":
            pts = 7
        elif etype == "stack_signal":
            if any(k in t for k in _LEGACY_STACK_HINTS):
                pts = 3
            elif any(k in t for k in _CLOUD_STACK_HINTS):
                pts = 2
        elif etype in ("pain_point", "product_mention", "summary"):
            if _kw_overlap(_sig_words(e.get("text")), tkw):
                pts = 4 if etype != "summary" else 2
        if pts:
            scored.append((pts, e))
    scored.sort(key=lambda x: -x[0])

    citations = [
        {
            "claim": e["text"],
            "url": e["url"],
            "quote": e.get("quote", ""),
            "page_title": e.get("page_title", ""),
            "type": e.get("type", ""),
        }
        for _pts, e in scored[:MAX_EVIDENCE_OUTPUT]
    ]
    if citations:
        reasons = "; ".join(f"{e['text']} ({e['url']})" for _pts, e in scored[:3])
        reasoning = f"Evidence match for '{target_product}': {reasons}."
    else:
        reasoning = f"No researched evidence links this company to '{target_product}'."

    return {"fit_score": int(fit), "reasoning": reasoning, "evidence": citations}


# --- Scoring ------------------------------------------------------------------

def score_one(lead: dict[str, Any]) -> dict[str, Any]:
    """Score a single researched lead with an evidence-cited justification."""
    scraped = lead.get("scraped_data", {}) or {}
    evidence = _pick_evidence(scraped)

    company = {
        "name": lead.get("company_name"),
        "website": lead.get("website"),
        "tech_stack": _pick_stack(lead.get("tech_stack", [])),
        "scraped": {
            "mission": scraped.get("mission", ""),
            "summary": scraped.get("summary", ""),
            "pain_points": scraped.get("pain_points", []),
            "product_mentions": scraped.get("product_mentions", []),
            "buying_signals": scraped.get("buying_signals", []),
            "evidence": evidence,
        },
    }
    user = json.dumps(
        {
            "target_product": lead.get("target_product") or "unknown",
            "company": company,
        },
        ensure_ascii=False,
        indent=2,
    )

    try:
        result = chat_json(
            load_prompt("scorer.md"),
            user,
            max_tokens=SCORER_MAX_TOKENS,
            response_format={"type": "json_object"},
        )

        fit_score = int(result.get("fit_score", 0))
        fit_score = max(0, min(100, fit_score))
        reasoning = str(result.get("reasoning", "")).strip()

        allowed_urls = {str(e.get("url", "")).rstrip("/") for e in evidence if e.get("url")}
        validated = _validate_evidence(result.get("evidence"), allowed_urls, evidence)
    except Exception as exc:
        # LLM down / rate-limited -> deterministic evidence-based fallback.
        print(
            f"[scorer] LLM failed for {lead.get('company_name')} ({exc}); "
            "using deterministic evidence fallback."
        )
        fb = _rule_based_score(lead.get("target_product") or "unknown", scraped, lead.get("tech_stack", []))
        fit_score = fb["fit_score"]
        reasoning = fb["reasoning"]
        validated = fb["evidence"]

    # Poor cold-outreach target penalty (deterministic, sourced).
    penalty, filter_findings = _filter_penalty(lead)
    if penalty:
        fit_score = max(0, fit_score + penalty)
        note = "Score adjusted for cold-outreach fit: " + "; ".join(
            f["reason"] for f in filter_findings
        )
        reasoning = f"{reasoning}\n\n{note}".strip() if reasoning else note

    # Deterministic confidence + human-readable top reasons.
    confidence = compute_confidence(scraped, lead.get("tech_stack", []))
    top_reasons = build_top_reasons(validated)

    scraped = dict(scraped)
    scraped["score_evidence"] = validated
    scraped["top_reasons"] = top_reasons
    scraped["confidence_score"] = confidence
    if filter_findings:
        scraped["filter_findings"] = filter_findings

    return db.update_lead(
        lead["id"],
        fit_score=fit_score,
        confidence_score=confidence,
        reasoning=reasoning,
        scraped_data=scraped,
        status=db.PENDING_REVIEW,
    )


def score(status: str = db.RESEARCHED, limit: int = 20) -> list[dict[str, Any]]:
    """Score all leads in `status` (default: researched)."""
    leads = db.get_leads_by_status(status, limit=limit)
    print(f"[scorer] Scoring {len(leads)} lead(s)")
    done: list[dict[str, Any]] = []
    for lead in leads:
        try:
            updated = score_one(lead)
            done.append(updated)
            print(
                f"[scorer] {updated.get('company_name')}: "
                f"fit={updated.get('fit_score')} conf={updated.get('confidence_score')} "
                f"-> pending_review"
            )
        except Exception as exc:
            db.update_lead(lead["id"], status=db.ERROR)
            print(f"[scorer] ERROR {lead.get('company_name')}: {exc}")
    return done


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Score researched leads")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    score(limit=args.limit)
