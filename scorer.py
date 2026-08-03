"""scorer.py - The Scorer agent.

Analyzes scraped company data vs. the Target Product using Gemma (via
OpenRouter) and assigns fit_score (1-100) + reasoning. Every score is backed
by an evidence trail: the LLM cites specific findings (claim + source URL +
verbatim quote) drawn ONLY from the researcher's `scraped_data["evidence"]`,
and the scorer persists those citations as `scraped_data["score_evidence"]`
after validating every URL against the provided evidence list (invented URLs
are dropped). Moves leads from 'researched' to 'pending_review'.

Scoring rubric lives in /prompts/scorer.md.
"""

from __future__ import annotations

import json
import re
from typing import Any

import database as db
from llm import chat, load_prompt, parse_json

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

_TYPE_PRIORITY = {
    "pain_point": 0, "stack_signal": 1, "product_mention": 2, "summary": 3, "email": 4,
}

# Per-type caps so the LLM sees a REPRESENTATIVE mix, not a flood of one type
# (e.g. 12 clinical pain points drowning out the stack signals).
_PICK_CAPS = {"pain_point": 6, "stack_signal": 6, "product_mention": 4, "summary": 3, "email": 1}


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
    raw_items: list[Any], allowed_urls: set[str]
) -> list[dict[str, str]]:
    """Keep only well-formed citations whose URL exists in the provided evidence."""
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
        validated.append({"claim": claim, "url": url, "quote": quote})
        if len(validated) >= MAX_EVIDENCE_OUTPUT:
            break
    return validated


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
    Calibration: legacy + cloud stack signals are the strongest migration
    signals; keyword-matched pain points/product mentions add evidence.
    """
    evidence = _pick_evidence(scraped)
    tkw = _sig_words(target_product)

    legacy: set[str] = set()
    cloud: set[str] = set()
    pain_hits = 0
    sum_hits = 0
    for e in evidence:
        t = (e.get("text") or "").lower()
        etype = e.get("type")
        if etype == "stack_signal":
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

    n_legacy, n_cloud = len(legacy), len(cloud)
    if n_legacy >= 2 and n_cloud >= 2:
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
        if etype == "stack_signal":
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
        {"claim": e["text"], "url": e["url"], "quote": e.get("quote", "")}
        for _pts, e in scored[:MAX_EVIDENCE_OUTPUT]
    ]
    if citations:
        reasons = "; ".join(f"{e['text']} ({e['url']})" for _pts, e in scored[:3])
        reasoning = f"Evidence match for '{target_product}': {reasons}."
    else:
        reasoning = f"No researched evidence links this company to '{target_product}'."

    return {"fit_score": int(fit), "reasoning": reasoning, "evidence": citations}


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
        reply = chat(
            load_prompt("scorer.md"),
            user,
            max_tokens=SCORER_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        result = parse_json(reply)

        fit_score = int(result.get("fit_score", 0))
        fit_score = max(0, min(100, fit_score))
        reasoning = str(result.get("reasoning", "")).strip()

        allowed_urls = {str(e.get("url", "")).rstrip("/") for e in evidence if e.get("url")}
        validated = _validate_evidence(result.get("evidence"), allowed_urls)
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

    scraped = dict(scraped)
    scraped["score_evidence"] = validated

    return db.update_lead(
        lead["id"],
        fit_score=fit_score,
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
                f"fit={updated.get('fit_score')} -> pending_review"
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
