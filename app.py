"""app.py - Conftech orchestrator: CLI + FastAPI background jobs.

Commands:
  python app.py init-db                          create tables
  python app.py pipeline --target "..." --limit 10   run full flow
  python app.py prospect --target "..." --limit 10   run Prospector only
  python app.py research --limit 20              run Deep Researcher only
  python app.py score --limit 20                 run Scorer only
  python app.py ghostwrite [--lead-id <uuid>]    run Ghostwriter (approved -> drafts)
  python app.py review                           human review of pending_review leads
  python app.py watch --interval 60              poll for approved leads -> ghostwrite
  python app.py serve --port 8000                FastAPI + background tasks

Status flow: raw -> researched -> pending_review -> approved -> drafted
                                          |-> rejected
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

def run_pipeline(target_product: str, limit: int = 10) -> dict[str, Any]:
    leads = prospect(target_product, limit=limit)
    research(limit=len(leads) or 10)
    score(limit=len(leads) or 10)
    stats = db.count_by_status()
    print(f"\n[pipeline] done. Status counts: {stats}")
    return {"prospected": len(leads), "status_counts": stats}


# --- FastAPI background jobs ------------------------------------------------

def build_api():
    """FastAPI app with background jobs (run: `python app.py serve`)."""
    from fastapi import BackgroundTasks, FastAPI
    from pydantic import BaseModel

    api = FastAPI(title="Conftech API", version="0.1.0")

    class PipelineRequest(BaseModel):
        target_product: str
        limit: int = 10

    class StatusUpdate(BaseModel):
        status: str  # approved | rejected

    def _run_pipeline_bg(target_product: str, limit: int) -> None:
        try:
            run_pipeline(target_product, limit)
        except Exception as exc:
            print(f"[api] pipeline job failed: {exc}")

    @api.post("/api/pipeline")
    def start_pipeline(req: PipelineRequest, bg: BackgroundTasks):
        bg.add_task(_run_pipeline_bg, req.target_product, req.limit)
        return {"job": "started", "target_product": req.target_product, "limit": req.limit}

    @api.get("/api/leads")
    def list_leads(status: str | None = None, limit: int = 50):
        if status:
            return db.get_leads_by_status(status, limit=limit)
        rows = []
        for s in db.ALL_STATUSES:
            rows.extend(db.get_leads_by_status(s, limit=limit))
        return rows

    @api.get("/api/leads/{lead_id}")
    def get_lead(lead_id: str):
        lead = db.get_lead(lead_id)
        if not lead:
            from fastapi import HTTPException

            raise HTTPException(404, "lead not found")
        return lead

    @api.post("/api/leads/{lead_id}/status")
    def update_status(lead_id: str, body: StatusUpdate):
        from fastapi import HTTPException

        lead = db.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "lead not found")
        if body.status not in (db.APPROVED, db.REJECTED):
            raise HTTPException(400, "status must be 'approved' or 'rejected'")
        db.set_status(lead_id, body.status)
        return {"id": lead_id, "status": body.status}

    @api.get("/api/drafts")
    def drafts(limit: int = 50):
        return db.list_drafts(limit=limit)

    @api.post("/api/ghostwrite/{lead_id}")
    def ghostwrite(lead_id: str):
        from fastapi import HTTPException

        from ghostwriter import ghostwrite_for_lead

        lead = db.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "lead not found")
        if lead.get("status") != db.APPROVED:
            raise HTTPException(400, f"lead status is {lead.get('status')}; expected approved")
        return ghostwrite_for_lead(lead)

    return api


# --- CLI --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="conftech", description="AI Intelligence Lead Platform")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Create leads + outreach_drafts tables")

    p = sub.add_parser("pipeline", help="Full flow: prospect -> research -> score")
    p.add_argument("--target", required=True)
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("prospect", help="Prospector only")
    p.add_argument("--target", required=True)
    p.add_argument("--limit", type=int, default=10)

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

    if args.cmd == "init-db":
        db.init_schema()
    elif args.cmd == "pipeline":
        run_pipeline(args.target, args.limit)
    elif args.cmd == "prospect":
        prospect(args.target, args.limit)
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

        uvicorn.run(build_api(), host="0.0.0.0", port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
