"""scorer.py - The Scorer agent.

Analyzes scraped company data vs. the Target Product using Gemma (via
OpenRouter) and assigns fit_score (1-100) + reasoning. Moves leads from
'researched' to 'pending_review'. Scoring rubric lives in /prompts/scorer.md.
"""

from __future__ import annotations

import json
from typing import Any

import database as db
from llm import chat, load_prompt, parse_json

SCORER_MAX_TOKENS = 250


def score_one(lead: dict[str, Any]) -> dict[str, Any]:
    """Score a single researched lead."""
    company = {
        "name": lead.get("company_name"),
        "website": lead.get("website"),
        "tech_stack": lead.get("tech_stack", []),
        "scraped": lead.get("scraped_data", {}),
    }
    user = json.dumps(
        {
            "target_product": lead.get("target_product") or "unknown",
            "company": company,
        },
        ensure_ascii=False,
        indent=2,
    )

    reply = chat(load_prompt("scorer.md"), user, max_tokens=SCORER_MAX_TOKENS)
    result = parse_json(reply)

    fit_score = int(result.get("fit_score", 0))
    fit_score = max(0, min(100, fit_score))
    reasoning = str(result.get("reasoning", "")).strip()

    return db.update_lead(
        lead["id"],
        fit_score=fit_score,
        reasoning=reasoning,
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
