"""webapp.py - FastAPI web UI + API for Conftech.

Served via `python app.py serve`. Jinja2 templates in /templates (Tailwind CDN),
the existing agents run in BackgroundTasks so the UI stays responsive.

Pages:
  GET  /                        dashboard (counts, recent leads/drafts, pipeline form)
  GET  /leads                    lead list (?status=, ?min_score=)
  GET  /leads/{id}               lead detail + approve/reject
  POST /leads/{id}/approve       approve (auto-drafts the email in the background)
  POST /leads/{id}/reject        reject
  GET  /drafts                   outreach drafts (preview/copy/delete)
  POST /pipeline/run             run the full pipeline in the background
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import database as db

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# --- Presentation helpers (exposed to templates as Jinja2 filters) -----------

STATUS_META: dict[str, tuple[str, str]] = {
    db.RAW: ("Raw", "bg-slate-500/15 text-slate-300 border-slate-500/30"),
    db.RESEARCHED: ("Researched", "bg-sky-500/15 text-sky-300 border-sky-500/30"),
    db.PENDING_REVIEW: ("Pending review", "bg-amber-500/15 text-amber-300 border-amber-500/30"),
    db.APPROVED: ("Approved", "bg-emerald-500/15 text-emerald-300 border-emerald-500/30"),
    db.REJECTED: ("Rejected", "bg-rose-500/15 text-rose-300 border-rose-500/30"),
    db.DRAFTED: ("Drafted", "bg-violet-500/15 text-violet-300 border-violet-500/30"),
    db.ERROR: ("Error", "bg-red-500/15 text-red-300 border-red-500/30"),
}


def status_label(status: str) -> str:
    return STATUS_META.get(status, (status, ""))[0]


def status_badge(status: str) -> str:
    """Tailwind classes for the status pill."""
    return STATUS_META.get(status, ("", "bg-slate-500/15 text-slate-300 border-slate-500/30"))[1]


def fit_color(score: int | None) -> str:
    if score is None:
        return "text-slate-400"
    if score >= 80:
        return "text-emerald-400"
    if score >= 65:
        return "text-amber-400"
    return "text-rose-400"


def fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return iso
    return dt.strftime("%b %d, %H:%M")


def _normalize_draft(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten the supabase `leads(...)` join into plain keys for templates."""
    lead = row.get("leads")
    if isinstance(lead, list):
        lead = lead[0] if lead else {}
    if not isinstance(lead, dict):
        lead = {}
    return {
        "id": row.get("id"),
        "lead_id": row.get("lead_id"),
        "subject": row.get("subject"),
        "email_body": row.get("email_body"),
        "created_at": row.get("created_at"),
        "company_name": (lead or {}).get("company_name") or "Unknown company",
        "website": (lead or {}).get("website"),
    }


def _run_pipeline_bg(target_product: str, limit: int) -> None:
    from prospector import prospect
    from researcher import research
    from scorer import score

    try:
        leads = prospect(target_product, limit=limit)
        research(limit=len(leads) or 10)
        score(limit=len(leads) or 10)
        print(f"[web] pipeline done: {db.count_by_status()}")
    except Exception as exc:
        print(f"[web] pipeline job failed: {exc}")


def _draft_email_bg(lead_id: str) -> None:
    try:
        from ghostwriter import ghostwrite_for_lead

        lead = db.get_lead(lead_id)
        if lead and lead.get("status") == db.APPROVED:
            ghostwrite_for_lead(lead)
    except Exception as exc:
        print(f"[web] ghostwrite failed for {lead_id}: {exc}")


# --- App factory --------------------------------------------------------------

def build_app() -> FastAPI:
    app = FastAPI(title="Conftech", version="0.2.0")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Jinja2 filter registration
    templates.env.filters["status_label"] = status_label
    templates.env.filters["status_badge"] = status_badge
    templates.env.filters["fit_color"] = fit_color
    templates.env.filters["fmt_dt"] = fmt_dt

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --- UI: dashboard -------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        counts = db.count_by_status()
        recent_leads: list[dict[str, Any]] = []
        for s in (db.PENDING_REVIEW, db.APPROVED, db.DRAFTED, db.RAW, db.RESEARCHED):
            for lead in db.get_leads_by_status(s, limit=4):
                recent_leads.append(lead)
        recent_leads.sort(key=lambda l: (l.get("fit_score") is None, l.get("fit_score") or 0), reverse=True)
        recent_drafts = [_normalize_draft(d) for d in db.list_drafts(limit=5)]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "counts": counts,
                "recent_leads": recent_leads[:8],
                "recent_drafts": recent_drafts,
                "active": "dashboard",
            },
        )

    # --- UI: leads list ------------------------------------------------------

    @app.get("/leads", response_class=HTMLResponse)
    def leads_page(
        request: Request,
        status: str | None = None,
        min_score: int | None = None,
    ):
        leads = db.list_leads(status=status, min_score=min_score, limit=100)
        # Best-fit first: scored leads sorted by fit_score desc, unscored at the bottom.
        leads.sort(key=lambda l: (l.get("fit_score") is None, l.get("fit_score") or 0), reverse=True)
        counts = db.count_by_status()
        notice = request.query_params.get("notice", "")
        return templates.TemplateResponse(
            request,
            "leads.html",
            {
                "leads": leads,
                "counts": counts,
                "current_status": status,
                "min_score": min_score,
                "notice": notice,
                "active": "leads",
            },
        )

    # --- UI: lead detail -----------------------------------------------------

    @app.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead_detail(request: Request, lead_id: str):
        lead = db.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "lead not found")
        draft = db.get_draft_for_lead(lead_id)
        counts = db.count_by_status()
        return templates.TemplateResponse(
            request,
            "lead_detail.html",
            {
                "lead": lead,
                "draft": draft,
                "counts": counts,
                "active": "leads",
            },
        )

    @app.post("/leads/{lead_id}/approve")
    def approve_lead(lead_id: str, bg: BackgroundTasks):
        lead = db.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "lead not found")
        if lead.get("status") != db.APPROVED:
            db.set_status(lead_id, db.APPROVED)
            bg.add_task(_draft_email_bg, lead_id)  # auto-draft the outreach email
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)

    @app.post("/leads/{lead_id}/reject")
    def reject_lead(lead_id: str):
        lead = db.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "lead not found")
        db.set_status(lead_id, db.REJECTED)
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)

    # --- UI: drafts ----------------------------------------------------------

    @app.get("/drafts", response_class=HTMLResponse)
    def drafts_page(request: Request):
        drafts = [_normalize_draft(d) for d in db.list_drafts(limit=100)]
        counts = db.count_by_status()
        return templates.TemplateResponse(
            request,
            "drafts.html",
            {"drafts": drafts, "counts": counts, "active": "drafts"},
        )

    @app.post("/drafts/{draft_id}/delete")
    def delete_draft(draft_id: str):
        db.delete_draft(draft_id)
        return RedirectResponse("/drafts", status_code=303)

    # --- UI: pipeline --------------------------------------------------------

    @app.post("/pipeline/run")
    def run_pipeline(
        target_product: str = Form(...),
        limit: int = Form(10),
        bg: BackgroundTasks = BackgroundTasks(),
    ):
        bg.add_task(_run_pipeline_bg, target_product, limit)
        return RedirectResponse("/leads?notice=started", status_code=303)

    # --- JSON API (kept from v0.1) ------------------------------------------

    class PipelineRequest(BaseModel):
        target_product: str
        limit: int = 10

    class StatusUpdate(BaseModel):
        status: str  # approved | rejected

    @app.post("/api/pipeline")
    def api_pipeline(req: PipelineRequest, bg: BackgroundTasks):
        bg.add_task(_run_pipeline_bg, req.target_product, req.limit)
        return {"job": "started", "target_product": req.target_product, "limit": req.limit}

    @app.get("/api/leads")
    def api_leads(status: str | None = None, limit: int = 50):
        if status:
            return db.get_leads_by_status(status, limit=limit)
        return db.list_leads(limit=limit)

    @app.get("/api/leads/{lead_id}")
    def api_get_lead(lead_id: str):
        lead = db.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "lead not found")
        return lead

    @app.post("/api/leads/{lead_id}/status")
    def api_update_status(lead_id: str, body: StatusUpdate):
        lead = db.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "lead not found")
        if body.status not in (db.APPROVED, db.REJECTED):
            raise HTTPException(400, "status must be 'approved' or 'rejected'")
        db.set_status(lead_id, body.status)
        return {"id": lead_id, "status": body.status}

    @app.get("/api/drafts")
    def api_drafts(limit: int = 50):
        return db.list_drafts(limit=limit)

    @app.post("/api/ghostwrite/{lead_id}")
    def api_ghostwrite(lead_id: str):
        from ghostwriter import ghostwrite_for_lead

        lead = db.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "lead not found")
        if lead.get("status") != db.APPROVED:
            raise HTTPException(400, f"lead status is {lead.get('status')}; expected approved")
        return ghostwrite_for_lead(lead)

    return app
