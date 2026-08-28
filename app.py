"""app.py - Conftech orchestrator: CLI + FastAPI background jobs.

Commands:
  python app.py init-db                          create tables
  python app.py hunt --target "..." --limit 20   INTENT-FIRST pipeline (recommended)
  python app.py pipeline --target "..." --limit 10   legacy company-fit pipeline
  python app.py prospect --target "..." --limit 10   legacy Prospector only
  python app.py research --limit 20              legacy Deep Researcher only
  python app.py score --limit 20                 legacy Scorer only
  python app.py ghostwrite [--lead-id <uuid>]    run Ghostwriter (approved -> drafts)
  python app.py review                           human review of pending_review leads
  python app.py watch --interval 60              poll for approved leads -> ghostwrite
  python app.py serve --port 8000                FastAPI web UI + API (webapp.py)

Status flow: raw -> researched -> pending_review -> approved -> drafted
                                          |-> rejected

NEW 'hunt' command runs the Intent-First pipeline (pipeline.py):
  Reddit / HN / LinkedIn / Job boards + TheirStack -> LLM enrich ->
  Decision-Maker finder -> Save leads with real human contact info.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import database as db
from ghostwriter import ghostwrite_batch
from prospector import prospect
from researcher import research
from scorer import score


# --- Human-in-the-loop ------------------------------------------------------

def review_leads(limit: int = 25) -> None:
    """List pending_review leads; approve/reject by lead id. Ctrl+C / 'q' quits."""
    leads = db.get_leads_by_status(db.PENDING_REVIEW, limit=limit)
    if not leads:
        print("No leads pending review.")
        return

    print(f"\n=== {len(leads)} lead(s) pending review ===\n")
    for i, lead in enumerate(leads, 1):
        print(
            f"[{i}] {lead.get('company_name')} | {lead.get('website')}\n"
            f"    fit={lead.get('fit_score')} | target={lead.get('target_product')}\n"
            f"    reasoning: {lead.get('reasoning') or '(none)'}"
        )

    while True:
        try:
            choice = input("\nLead number to review (a<#> approve, r<#> reject, q quit): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return
        if choice in ("q", ""):
            return
        if len(choice) < 2 or choice[0] not in "ar":
            print("Format: a3 (approve #3), r5 (reject #5), q (quit)")
            continue
        action, idx = choice[0], choice[1:]
        try:
            lead = leads[int(idx) - 1]
        except (ValueError, IndexError):
            print(f"Unknown lead number: {idx}")
            continue
        new_status = db.APPROVED if action == "a" else db.REJECTED
        db.set_status(lead["id"], new_status)
        print(f"-> {lead.get('company_name')} marked {new_status}")


# --- Watch loop (ghostwriter trigger) ---------------------------------------

def watch(interval: int = 60) -> None:
    """Poll Supabase for approved leads and draft emails for them."""
    print(f"[watch] Monitoring for approved leads every {interval}s (Ctrl+C to stop)...")
    while True:
        try:
            drafts = ghostwrite_batch(status=db.APPROVED)
            if drafts:
                print(f"[watch] Drafted {len(drafts)} email(s)")
        except Exception as exc:
            print(f"[watch] error: {exc}")
        time.sleep(interval)


# --- Pipeline ---------------------------------------------------------------

def run_pipeline(
    target_product: str,
    limit: int = 10,
    buyer_persona: str = "",
) -> dict[str, Any]:
    result = prospect(target_product, limit=limit, buyer_persona=buyer_persona)
    new_leads = result["new"]
    research(limit=len(new_leads) or 10)
    score(limit=len(new_leads) or 10)
    stats = db.count_by_status()
    print(f"\n[pipeline] done. Status counts: {stats}")
    return {
        "prospected": len(new_leads),
        "known": len(result["known"]),
        "excluded": len(result["excluded"]),
        "status_counts": stats,
    }


# --- Web UI / API server -----------------------------------------------------
# The FastAPI app (web UI + JSON API) lives in webapp.py; serve runs it.
#   python app.py serve --port 8000

# --- CLI --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="conftech", description="AI Intelligence Lead Platform")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Create leads + outreach_drafts tables")

    p = sub.add_parser("hunt", help="INTENT-FIRST pipeline (recommended): find people asking for the service")
    p.add_argument("--target", required=True)
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--persona", default="", help="Optional buyer persona (industry/size hints)")
    p.add_argument("--verify-emails", action="store_true", help="SMTP-verify candidate emails (slower)")

    p = sub.add_parser("pipeline", help="Legacy company-fit flow: prospect -> research -> score")
    p.add_argument("--target", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument(
        "--persona",
        default="",
        help="Free-form buyer persona (industry, size, existing tech posture). "
             "Used to constrain the prospector and the scorer.",
    )

    p = sub.add_parser("prospect", help="Prospector only")
    p.add_argument("--target", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--persona", default="")

    p = sub.add_parser("research", help="Deep Researcher only")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("score", help="Scorer only")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("ghostwrite", help="Ghostwriter for approved leads")
    p.add_argument("--lead-id")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("review", help="Human review of pending_review leads")
    p.add_argument("--limit", type=int, default=25)

    p = sub.add_parser("watch", help="Poll for approved leads -> draft emails")
    p.add_argument("--interval", type=int, default=60)

    p = sub.add_parser("serve", help="FastAPI server with background jobs")
    p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    try:
        if args.cmd == "init-db":
            db.init_schema()
        elif args.cmd == "hunt":
            from pipeline import run_intent_pipeline
            run_intent_pipeline(
                args.target,
                buyer_persona=args.persona,
                limit=args.limit,
                verify_emails=args.verify_emails,
            )
        elif args.cmd == "pipeline":
            run_pipeline(args.target, args.limit, buyer_persona=args.persona)
        elif args.cmd == "prospect":
            prospect(args.target, args.limit, buyer_persona=args.persona)
        elif args.cmd == "research":
            research(limit=args.limit)
        elif args.cmd == "score":
            score(limit=args.limit)
        elif args.cmd == "ghostwrite":
            if args.lead_id:
                from ghostwriter import ghostwrite_for_lead

                lead = db.get_lead(args.lead_id)
                if not lead:
                    print(f"Lead {args.lead_id} not found")
                    return 1
                draft = ghostwrite_for_lead(lead)
                print(f"Subject: {draft.get('subject')}\n\n{draft.get('email_body')}")
            else:
                ghostwrite_batch(limit=args.limit)
        elif args.cmd == "review":
            review_leads(limit=args.limit)
        elif args.cmd == "watch":
            watch(interval=args.interval)
        elif args.cmd == "serve":
            import uvicorn

            import webapp

            uvicorn.run(webapp.build_app(), host="0.0.0.0", port=args.port)
    except db.TableNotReadyError as exc:
        print(f"\n[conftech] {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
