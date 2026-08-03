"""ghostwriter.py - The Human-in-the-loop Ghostwriter.

For APPROVED leads, writes a hyper-personalized cold email (subject + body)
with Gemma, referencing the company's actual tech stack and the scorer's
reasoning. Drafts are saved to outreach_drafts and the lead moves to 'drafted'.
Email guidelines live in /prompts/ghostwriter.md.
"""

from __future__ import annotations

import json
from typing import Any

import database as db
from llm import chat, load_prompt, parse_json

GHOSTWRITER_MAX_TOKENS = 450


def ghostwrite_for_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """Write + persist a draft for one approved lead."""
    scraped = lead.get("scraped_data", {}) or {}
    company = {
        "name": lead.get("company_name"),
        "website": lead.get("website"),
        "tech_stack": lead.get("tech_stack", []),
        "scraped": scraped,
    }
    user = json.dumps(
        {
            "target_product": lead.get("target_product") or "unknown",
            "company": company,
            "scorer_reasoning": lead.get("reasoning", ""),
            "evidence": scraped.get("score_evidence") or scraped.get("evidence") or [],
        },
        ensure_ascii=False,
        indent=2,
    )

    reply = chat(load_prompt("ghostwriter.md"), user, max_tokens=GHOSTWRITER_MAX_TOKENS)
    result = parse_json(reply)

    subject = str(result.get("subject", "")).strip()
    body = str(result.get("email_body", "")).strip()
    if not body:
        raise RuntimeError("Ghostwriter returned empty email_body")

    draft = db.insert_draft(lead["id"], subject=subject, email_body=body)
    db.set_status(lead["id"], db.DRAFTED)
    return draft


def ghostwrite_batch(status: str = db.APPROVED, limit: int = 20) -> list[dict[str, Any]]:
    """Draft emails for all leads in `status` (default: approved)."""
    leads = db.get_leads_by_status(status, limit=limit)
    print(f"[ghostwriter] Drafting for {len(leads)} approved lead(s)")
    drafts: list[dict[str, Any]] = []
    for lead in leads:
        try:
            draft = ghostwrite_for_lead(lead)
            drafts.append(draft)
            print(f"[ghostwriter] draft saved for {lead.get('company_name')} -> drafted")
        except Exception as exc:
            print(f"[ghostwriter] ERROR {lead.get('company_name')}: {exc}")
    return drafts


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Draft emails for approved leads")
    parser.add_argument("--lead-id", help="Draft for a single lead id")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    if args.lead_id:
        lead = db.get_lead(args.lead_id)
        if not lead:
            raise SystemExit(f"Lead {args.lead_id} not found")
        if lead.get("status") != db.APPROVED:
            raise SystemExit(f"Lead is status={lead.get('status')}; expected approved")
        draft = ghostwrite_for_lead(lead)
        print(f"Subject: {draft.get('subject')}\n\n{draft.get('email_body')}")
    else:
        ghostwrite_batch(limit=args.limit)
