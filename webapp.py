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

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import database as db

# Request bodies live at module level: `from __future__ import annotations`
# turns annotations into strings, and models defined inside build_app() can't
# be resolved from module scope when FastAPI/Pydantic builds the OpenAPI schema
# (PydanticUserError: ... not fully defined -> /openapi.json 500).
class PipelineRequest(BaseModel):
    target_product: str
    limit: int = 10


class StatusUpdate(BaseModel):
    status: str  # approved | rejected


class BulkStatusUpdate(BaseModel):
    status: str  # approved | rejected
    ids: list[str] = []


BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


# --- Per-incident guidance ("what to do") -----------------------------------
# When the pipeline hits an upstream limit (TheirStack 402, Serper 400 quoted
# query, LLM 500, ...) we surface a SPECIFIC fix hint in the web banner instead
# of a generic "something went wrong". Adding a new upstream just means adding
# a new regex here.

_INCIDENT_HINTS: list[tuple[str, str]] = [
    # (regex matched against incident.error / incident.message, friendly hint)
    (r"theirstack.*402|402.*theirstack|theirstack.*no credits|theirstack.*payment",
     "TheirStack returned 402 (no credits). Add credits at theirstack.com or wait until the monthly reset."),
    (r"theirstack.*422|422.*theirstack",
     "TheirStack rejected the search payload (422). The prospector's tech slugs are no longer in TheirStack's catalog; the next run will retry with the keyword fallback."),
    (r"serper.*pattern not allowed|pattern not allowed.*serper|serper.*400",
     "Serper free tier rejected this query (\"pattern not allowed for free accounts\"). DuckDuckGo is the primary search; Serper is fallback only. The query is fine - just retry later or upgrade Serper."),
    (r"serper.*429|429.*serper",
     "Serper free tier is rate-limited (429). The pipeline already retried; wait a few minutes before running again, or upgrade."),
    (r"no module named ['\"]ddgs|no module named ['\"]duckduckgo_search",
     "DuckDuckGo is not importable in this Python environment. Start the server with `.venv\\Scripts\\python.exe app.py serve` (not `py app.py serve`) so the ddgs package loads."),
    (r"llm gateway 5\d\d|gateway 500|gateway 502|gateway 503",
     "The LLM gateway returned a 5xx. All configured models failed; this is an upstream outage, not a bug. Try again in a few minutes."),
    (r"llm gateway 429|gateway 429",
     "The LLM gateway is rate-limiting this account. Wait a few minutes before running again."),
    (r"no payment method|creditserror",
     "The OpenCode Zen account on this key has no payment method. The free models (mimo-v2.5-free, hy3-free, nemotron-3-ultra-free) still work; paid models like grok-build-0.1 do not."),
    (r"gmi-serving|api\.gmi-serving\.com|minimax",
     "The primary LLM (GMI Cloud) failed. The pipeline should have fallen back to the OpenCode Zen chain automatically; check the chain order in llm.py / .env."),
    (r"model .* is not supported|model .* unavailable|endpoint is unavailable",
     "A model in the configured chain is currently unavailable. The pipeline should have fallen back to the next model automatically; if it didn't, edit LLM_MODEL / LLM_FALLBACK_MODEL in .env to a known-working free model."),
    (r"already exist in the database",
     "All companies the prospector found for this target are already in your database (status: pending_review or rejected). Try a different target product, a higher limit, or a more specific search term."),
    (r"no leads located",
     "The prospector's upstream search returned zero results for this target. The target text might be too specific or misspelled. Try a broader product name or a higher limit."),
]


def hint_for(error_text: str) -> str:
    """Return the matching user-friendly hint for an upstream error, or ''."""
    if not error_text:
        return ""
    for pattern, hint in _INCIDENT_HINTS:
        if re.search(pattern, error_text, re.IGNORECASE):
            return hint
    return ""


def classify_incident_kind(error_text: str) -> str:
    """Bucket an incident into a stable kind for analytics + UI grouping."""
    if not error_text:
        return "unknown"
    low = error_text.lower()
    if "theirstack" in low or "their stack" in low:
        if "402" in low or "no credits" in low or "payment" in low:
            return "theirstack_402"
        if "422" in low:
            return "theirstack_422"
        return "theirstack_other"
    if "serper" in low:
        if "429" in low:
            return "serper_429"
        if "pattern not allowed" in low or "400" in low:
            return "serper_quoted_query"
        return "serper_other"
    if "no module named" in low and ("ddgs" in low or "duckduckgo" in low):
        return "ddgs_missing"
    if "llm gateway" in low or "model" in low and "not supported" in low:
        if "5" in low.split("gateway ", 1)[-1][:1]:
            return "llm_5xx"
        if "429" in low:
            return "llm_rate_limit"
        if "no payment" in low or "creditserror" in low:
            return "llm_payment_required"
        return "llm_other"
    return "other"


def _banner_payload() -> dict[str, Any] | None:
    """Return the data the templates need to render the persistent error
    banner, or None when there's nothing to show."""
    if _JOB_STATE.get("running"):
        return None
    if _JOB_STATE.get("acknowledged"):
        return None
    incidents = _JOB_STATE.get("incidents") or []
    error = _JOB_STATE.get("error")
    if not incidents and not error:
        return None
    # Pick the most-severe incident for the headline; everything else is
    # listed in `incidents` for the expandable section.
    headline = None
    if incidents:
        first = incidents[0]
        headline = {
            "kind": first.get("kind"),
            "source": first.get("source"),
            "stage": first.get("stage"),
            "error": first.get("error"),
        }
    return {
        "headline": headline,
        "incidents": incidents,
        "error": error,
        "message": _JOB_STATE.get("message"),
        "kind": _JOB_STATE.get("last_error_kind"),
        "hint": _JOB_STATE.get("last_error_hint") or "",
    }

# --- Presentation helpers (exposed to templates as Jinja2 filters) -----------

# Status pill classes are owned by static/style.css (.st-* modifiers on the
# .status base class). System: in-flow stages (raw -> researched ->
# pending_review -> drafted) use the accent hue with increasing fill;
# outcomes are semantic (approved = emerald, rejected/error = rose/red).
STATUS_META: dict[str, tuple[str, str]] = {
    db.RAW: ("Raw", "st-raw"),
    db.RESEARCHED: ("Researched", "st-researched"),
    db.PENDING_REVIEW: ("Pending review", "st-pending"),
    db.APPROVED: ("Approved", "st-approved"),
    db.REJECTED: ("Rejected", "st-rejected"),
    db.DRAFTED: ("Drafted", "st-drafted"),
    db.ERROR: ("Error", "st-error"),
}


def status_label(status: str) -> str:
    return STATUS_META.get(status, (status, ""))[0]


def status_badge(status: str) -> str:
    """Status-pill modifier class for the .status base (see style.css)."""
    return STATUS_META.get(status, ("", "st-raw"))[1]


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


# --- Pipeline job tracking (in-memory; single-user local tool) ----------------
# The UI polls /api/job/status so the user can see what the background
# pipeline run is doing instead of staring at a blank redirect. We also keep
# `incidents` and `last_error_kind` around so when the run finishes with a
# problem (LLM 5xx, TheirStack 402, Serper 400, ...) the banner stays red
# and points at the specific failure with a fix hint, instead of silently
# flipping back to "no new leads found".

_JOB_STATE: dict[str, Any] = {
    "running": False,
    "incidents": [],
    "last_error_kind": None,
    "last_error_hint": None,
    "acknowledged": False,
    "events": [],
}

# Cap the in-memory event log so a long pipeline doesn't OOM the FastAPI
# process. The UI only needs the most recent events; older ones fall off.
_MAX_EVENTS = 200


def _record_event(
    agent: str,
    kind: str,
    summary: str,
    detail: Any = None,
    lead_id: str | None = None,
) -> None:
    """Append one structured event to the in-memory activity stream.

    Any agent can call this (via webapp._record_event) to publish a step
    for the live dashboard. `kind` is a free-form short tag (e.g. "llm_call",
    "theirstack_query", "page_crawled", "evidence_added", "score"); `summary`
    is the human-readable one-liner shown in the activity panel; `detail`
    is the structured payload (dicts/lists/strings) that the user can expand
    in the UI to see exactly what the agent did.
    """
    _JOB_STATE.setdefault("events", []).append(
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "agent": agent,
            "kind": kind,
            "summary": summary,
            "detail": detail,
            "lead_id": lead_id,
        }
    )
    # Trim the buffer. Drop the oldest non-summary events first, then any.
    if len(_JOB_STATE["events"]) > _MAX_EVENTS:
        _JOB_STATE["events"] = _JOB_STATE["events"][-_MAX_EVENTS:]


def _job_start(target_product: str, limit: int) -> None:
    _JOB_STATE.clear()
    _JOB_STATE.update(
        {
            "running": True,
            "target_product": target_product,
            "limit": limit,
            "started_at": datetime.now().isoformat(),
            "finished_at": None,
            "stage": "prospecting",
            "stage_label": "Finding companies",
            "message": "",
            "leads_found": 0,
            "leads_known": 0,
            "leads_excluded": 0,
            "error": None,
            "incidents": [],
            "last_error_kind": None,
            "last_error_hint": None,
            "acknowledged": False,
            "events": [],
        }
    )


def _job_update(stage: str, stage_label: str, **extra: Any) -> None:
    _JOB_STATE["stage"] = stage
    _JOB_STATE["stage_label"] = stage_label
    _JOB_STATE.update(extra)


def _record_incident(stage: str, source: str, exc: Exception) -> None:
    """Append a structured incident so the UI can show the SPECIFIC failure
    (TheirStack 402, Serper 400, LLM 500, ...) instead of a generic message."""
    from prospector import TheirStackQuotaError  # imported lazily to avoid cycle
    msg = str(exc)
    if isinstance(exc, TheirStackQuotaError):
        # All TheirStack keys are depleted / rate-limited. The prospector
        # already records per-call `theirstack_error` events; this is the
        # "final" incident with the user-facing summary and a friendly hint.
        kind = "theirstack_all_keys_exhausted"
        if not _JOB_STATE.get("last_error_hint"):
            _JOB_STATE["last_error_hint"] = (
                "All configured TheirStack keys are out of credits (402) or "
                "rate-limited (429). Add more keys to THEIRSTACK_API_KEY_2 / _3 "
                "in .env, or top up credits at theirstack.com. The pipeline will "
                "keep trying to find leads via the Serper/DuckDuckGo fallback path."
            )
    else:
        kind = classify_incident_kind(f"{source} {msg}")
    incident = {
        "stage": stage,
        "source": source,
        "error": msg,
        "kind": kind,
        "ts": datetime.now().isoformat(),
    }
    _JOB_STATE.setdefault("incidents", []).append(incident)
    _JOB_STATE["last_error_kind"] = kind
    if not _JOB_STATE.get("last_error_hint"):
        _JOB_STATE["last_error_hint"] = hint_for(msg) or hint_for(source)


def _job_finish(error: str | None = None) -> None:
    _JOB_STATE["running"] = False
    _JOB_STATE["finished_at"] = datetime.now().isoformat()
    _JOB_STATE["error"] = error
    # If the job ended with at least one incident, derive a single-line summary
    # the UI can show in the banner even when no exception was re-raised
    # (e.g. prospector hit a 402 internally and returned an empty result).
    if _JOB_STATE.get("incidents") and not error:
        kinds = sorted({i["kind"] for i in _JOB_STATE["incidents"]})
        _JOB_STATE["error"] = (
            f"Pipeline hit: {', '.join(kinds)}. See banner for details."
        )
        if not _JOB_STATE.get("last_error_hint"):
            _JOB_STATE["last_error_hint"] = _JOB_STATE["incidents"][0].get("error", "")


def _run_pipeline_bg(target_product: str, limit: int, buyer_persona: str = "") -> None:
    from prospector import NoNewLeadsError, prospect
    from researcher import research
    from scorer import score
    from search import SerperQuotaError

    _job_start(target_product, limit)
    _JOB_STATE["buyer_persona"] = buyer_persona
    try:
        _job_update("prospecting", "Finding companies")
        try:
            result = prospect(target_product, limit=limit, buyer_persona=buyer_persona)
        except SerperQuotaError as exc:
            _record_incident("prospecting", "serper", exc)
            _job_finish(error=str(exc))
            return
        except Exception as exc:
            _record_incident("prospecting", "prospector", exc)
            raise

        new_leads = result["new"]
        known_leads = result["known"]
        excluded = result["excluded"]
        _JOB_STATE["leads_found"] = len(new_leads)
        _JOB_STATE["leads_known"] = len(known_leads)
        _JOB_STATE["leads_excluded"] = len(excluded)

        if not new_leads:
            # prospect() raised NoNewLeadsError before we got here only if it
            # also found companies; if even the search returned nothing, this
            # is a quiet no-op. Either way, the message should be honest.
            if known_leads:
                _job_update(
                    "prospecting",
                    "Finding companies",
                    message=(
                        f"No new leads for '{target_product}' - all "
                        f"{len(known_leads)} companies already in DB. "
                        "Try a different target or increase the limit."
                    ),
                )
            else:
                _job_update(
                    "prospecting",
                    "Finding companies",
                    message=(
                        f"No leads located for '{target_product}'. "
                        "Upstream search returned no matches."
                    ),
                )
            _job_finish()
            print(f"[web] pipeline done (no new leads): {db.count_by_status()}")
            return

        _job_update(
            "prospecting",
            "Finding companies",
            message=(
                f"Found {len(new_leads)} new + {len(known_leads)} already in DB "
                f"for '{target_product}'"
            ),
        )
        _job_update("researching", "Deep-researching websites")
        try:
            research(limit=len(new_leads))
        except Exception as exc:
            _record_incident("researching", "researcher", exc)
            raise
        _job_update("scoring", "Scoring fit")
        try:
            score(limit=len(new_leads))
        except Exception as exc:
            _record_incident("scoring", "scorer", exc)
            raise

        # Qualified leads check: only companies with verified buying signals / pain
        # points (fit >= 60 and status == pending_review) are counted as qualified leads.
        if new_leads:
            try:
                rescored = db.get_client().table("leads").select("id, status, fit_score").in_("id", [l["id"] for l in new_leads]).execute()
                qualified = [r for r in (rescored.data or []) if r.get("status") == db.PENDING_REVIEW and (r.get("fit_score") or 0) >= 60]
            except Exception:
                qualified = []
            _JOB_STATE["leads_found"] = len(qualified)

            if not qualified and len(new_leads) > 0:
                msg = (
                    f"Prospector analyzed {len(new_leads)} candidate(s) for '{target_product}', "
                    f"but none met the qualification threshold (no credible buying signals or pain points found). "
                    f"All {len(new_leads)} candidates were marked as rejected/unqualified rather than filling the queue with weak leads."
                )
                _record_event(
                    agent="scorer",
                    kind="no_qualified_leads",
                    summary="No qualified leads found — candidates lacked verified buying signals or pain points",
                    detail={"new_candidates": len(new_leads), "qualified_count": 0, "target_product": target_product},
                )
                _job_update("scoring", "Completed", message=msg)
                _job_finish(error=msg)
                print(f"[web] no qualified leads: {msg}")
                return
            else:
                _job_update(
                    "scoring",
                    "Completed",
                    message=f"Pipeline complete: {len(qualified)} qualified lead(s) surfaced for '{target_product}' ({len(new_leads) - len(qualified)} unqualified/rejected).",
                )

        _job_finish()
        print(f"[web] pipeline done: {db.count_by_status()}")
    except NoNewLeadsError as exc:
        # prospector already printed each DUPLICATE with its current status; we
        # just need the headline count for the web banner. The message format
        # is "All N companies found for '...' already exist in the database..."
        msg = str(exc)
        import re as _re
        m = _re.search(r"All (\d+) companies", msg)
        if m:
            _JOB_STATE["leads_known"] = int(m.group(1))
        _job_update(
            "prospecting",
            "Finding companies",
            message=msg,
        )
        _job_finish(error=msg)
        print(f"[web] no new leads: {msg}")
    except Exception as exc:
        _job_finish(error=str(exc))
        print(f"[web] pipeline job failed: {exc}")


def _draft_email_bg(lead_id: str) -> None:
    try:
        from ghostwriter import ghostwrite_for_lead

        lead = db.get_lead(lead_id)
        if lead and lead.get("status") == db.APPROVED:
            ghostwrite_for_lead(lead)
    except Exception as exc:
        print(f"[web] ghostwrite failed for {lead_id}: {exc}")


# ─── Intent-Hunt background job ───────────────────────────────────────────────
# Runs the new intent-first pipeline (Reddit/HN/LinkedIn/jobs → enrich →
# decision-maker finder → score → save). Uses the same _JOB_STATE so the
# dashboard's status widget + activity panel Just Work for both pipelines.

def _run_hunt_bg(target_product: str, limit: int, verify_emails: bool = False) -> None:
    from pipeline import run_intent_pipeline

    _job_start(target_product, limit)
    _JOB_STATE["mode"] = "hunt"
    _JOB_STATE["leads_excluded"] = 0
    try:
        _job_update("hunting", "Hunting intent signals", message=f"Scraping Reddit, HN, LinkedIn, job boards for '{target_product}'...")
        result = run_intent_pipeline(
            target_product,
            limit=limit,
            verify_emails=verify_emails,
        )
        n = result.get("leads_saved", 0)
        n_sigs = result.get("signals_found", 0)
        n_dms = result.get("decision_makers_found", 0)
        _JOB_STATE["leads_found"] = n
        _JOB_STATE["leads_known"] = 0
        _JOB_STATE["leads_excluded"] = 0
        if n == 0:
            _job_update(
                "hunting",
                "Hunt complete",
                message=(
                    f"No leads saved for '{target_product}' from {n_sigs} intent signal(s). "
                    "Try a different target product or raise the limit."
                ),
            )
        else:
            _job_update(
                "hunting",
                "Hunt complete",
                message=(
                    f"Hunt complete: {n} lead(s) saved from {n_sigs} intent signal(s) "
                    f"({n_dms} decision-maker(s) found). All in pending_review with auto-drafted emails."
                ),
            )
        _job_finish()
        print(f"[web] hunt done: {n} leads saved (signals={n_sigs}, dms={n_dms})")
    except Exception as exc:
        _record_incident("hunting", "hunt", exc)
        _job_finish(error=str(exc))
        print(f"[web] hunt failed: {exc}")


# --- App factory --------------------------------------------------------------

def build_app() -> FastAPI:
    # Startup probe: print exactly which search backend loaded. A common
    # breakage is running `py app.py serve` (Anaconda Python) instead of
    # `.venv\Scripts\python.exe app.py serve`; the Anaconda interpreter has
    # no `ddgs` / `duckduckgo_search` and the server silently falls back to
    # Serper-only, which immediately hits the "Query pattern not allowed"
    # free-tier rejection. Make this loud.
    try:
        from ddgs import DDGS as _DDGS  # type: ignore
        ddgs_ver = getattr(_DDGS, "__version__", "unknown")
        print(f"[startup] Search backend: DuckDuckGo (ddgs {ddgs_ver}) + Serper fallback")
    except Exception as exc:
        print(
            "[startup] WARNING: ddgs not importable - DuckDuckGo search will be "
            f"skipped ({exc}). Run the server with `.venv\\Scripts\\python.exe "
            "app.py serve` so the ddgs package actually loads."
        )

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
        # Per-product totals + pending_review counts for the dashboard grid.
        products = db.list_target_products()
        pending_by_product: dict[str, int] = {}
        if products:
            for r in db.list_leads(status=db.PENDING_REVIEW, limit=1000):
                name = r.get("target_product") or "Untagged"
                pending_by_product[name] = pending_by_product.get(name, 0) + 1
        recent_leads: list[dict[str, Any]] = []
        for s in (db.PENDING_REVIEW, db.APPROVED, db.DRAFTED, db.RAW, db.RESEARCHED):
            for lead in db.get_leads_by_status(s, limit=4):
                recent_leads.append(lead)
        recent_leads.sort(key=lambda l: (l.get("fit_score") is None, l.get("fit_score") or 0), reverse=True)
        recent_drafts = [_normalize_draft(d) for d in db.list_drafts(limit=5)]
        job_banner = _banner_payload()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "counts": counts,
                "products": products,
                "pending_by_product": pending_by_product,
                "recent_leads": recent_leads[:8],
                "recent_drafts": recent_drafts,
                "active": "dashboard",
                "job_banner": job_banner,
            },
        )

    # --- UI: leads list ------------------------------------------------------

    @app.get("/leads", response_class=HTMLResponse)
    def leads_page(
        request: Request,
        status: str | None = None,
        min_score: int | None = None,
        target_product: str | None = None,
        sort: str = "fit_desc",
        q: str | None = None,
        min_confidence: int | None = None,
        has_dm: str | None = None,
        matches_persona: str | None = None,
    ):
        # Coerce "on"/"true" → True, "off"/None → None.
        def _truthy(v: str | None) -> bool | None:
            if v is None:
                return None
            return str(v).lower() in ("1", "true", "on", "yes")

        leads = db.list_leads(
            status=status,
            min_score=min_score,
            target_product=target_product,
            limit=200,
            sort=sort,
            min_confidence=min_confidence,
            has_dm=_truthy(has_dm),
            matches_persona=_truthy(matches_persona),
        )

        # Search happens client-side too, but doing it server-side keeps the URL
        # shareable and the page count accurate. A reviewer who shares a link
        # with a colleague gets the same view.
        if q:
            needle = q.lower().strip()
            def _hay(lead: dict[str, Any]) -> str:
                cp = lead.get("company_profile") or {}
                dm = (cp.get("decision_makers") or [{}])[0] if cp else {}
                parts = [
                    lead.get("company_name") or "",
                    lead.get("website") or "",
                    lead.get("contact_email") or "",
                    lead.get("reasoning") or "",
                    lead.get("target_product") or "",
                    dm.get("name") or "",
                    dm.get("email") or "",
                    dm.get("title") or "",
                ]
                return " ".join(p for p in parts if p).lower()
            leads = [l for l in leads if needle in _hay(l)]

        # Best-fit first: scored leads sorted by fit_score desc, unscored at the bottom.
        # (Server already orders; this guarantees the tie-break when fit_score is null.)
        leads.sort(key=lambda l: (l.get("fit_score") is None, -(l.get("fit_score") or 0)))
        counts = db.count_by_status()
        products = db.list_target_products()
        notice = request.query_params.get("notice", "")
        job_banner = _banner_payload()
        return templates.TemplateResponse(
            request,
            "leads.html",
            {
                "leads": leads,
                "counts": counts,
                "products": list(products.keys()),
                "leads_by_product": products,
                "current_status": status,
                "min_score": min_score,
                "min_confidence": min_confidence,
                "target_product": target_product,
                "sort": sort,
                "q": q or "",
                "has_dm": _truthy(has_dm),
                "matches_persona": _truthy(matches_persona),
                "notice": notice,
                "active": "leads",
                "job_banner": job_banner,
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
        job_banner = _banner_payload()
        return templates.TemplateResponse(
            request,
            "lead_detail.html",
            {
                "lead": lead,
                "draft": draft,
                "counts": counts,
                "active": "leads",
                "job_banner": job_banner,
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
        job_banner = _banner_payload()
        return templates.TemplateResponse(
            request,
            "drafts.html",
            {"drafts": drafts, "counts": counts, "active": "drafts", "job_banner": job_banner},
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
        buyer_persona: str = Form(""),
        bg: BackgroundTasks = BackgroundTasks(),
    ):
        bg.add_task(_run_pipeline_bg, target_product, limit, buyer_persona)
        return RedirectResponse("/leads?notice=started", status_code=303)

    @app.post("/hunt/run")
    def run_hunt(
        target_product: str = Form(...),
        limit: int = Form(15),
        verify_emails: str = Form(""),
        bg: BackgroundTasks = BackgroundTasks(),
    ):
        verify = str(verify_emails).lower() in ("1", "true", "yes", "on")
        bg.add_task(_run_hunt_bg, target_product, limit, verify)
        return RedirectResponse("/leads?notice=hunt_started", status_code=303)

    @app.post("/api/hunt")
    def api_hunt(req: PipelineRequest, verify_emails: bool = False, bg: BackgroundTasks = BackgroundTasks()):
        bg.add_task(_run_hunt_bg, req.target_product, req.limit, verify_emails)
        return {
            "job": "started",
            "mode": "hunt",
            "target_product": req.target_product,
            "limit": req.limit,
        }

    # --- JSON API (kept from v0.1) ------------------------------------------

    @app.post("/api/pipeline")
    def api_pipeline(req: PipelineRequest, bg: BackgroundTasks):
        bg.add_task(_run_pipeline_bg, req.target_product, req.limit, req.buyer_persona)
        return {
            "job": "started",
            "target_product": req.target_product,
            "limit": req.limit,
            "buyer_persona": req.buyer_persona,
        }

    @app.get("/api/leads")
    def api_leads(status: str | None = None, target_product: str | None = None, limit: int = 50):
        if status:
            leads = db.get_leads_by_status(status, limit=limit)
        else:
            leads = db.list_leads(status=None, target_product=target_product, limit=limit)
        if target_product:
            leads = [l for l in leads if l.get("target_product") == target_product]
        return leads

    @app.get("/api/job/status")
    def api_job_status():
        # The activity panel polls /api/job/events separately (faster cadence),
        # but we also include the last few events here so any client that only
        # hits /status still sees the agent's recent activity.
        snapshot = dict(_JOB_STATE)
        events = snapshot.get("events") or []
        snapshot["events"] = events[-50:]  # cap to last 50 for the lightweight poll
        return snapshot

    @app.get("/api/job/events")
    def api_job_events(since: int = 0):
        """Return the agent activity stream (all events, or events after index
        `since` for incremental polling).

        Each event is {ts, agent, kind, summary, detail, lead_id}. The UI
        shows the most recent first; `detail` is the structured payload the
        user can expand (LLM prompts, API requests, evidence rows, scores).
        """
        events = list(_JOB_STATE.get("events") or [])
        return {
            "running": _JOB_STATE.get("running", False),
            "total": len(events),
            "events": events[since:],
            "stage": _JOB_STATE.get("stage"),
            "stage_label": _JOB_STATE.get("stage_label"),
            "message": _JOB_STATE.get("message"),
        }

    @app.get("/api/job/last_error")
    def api_job_last_error():
        """Return the most recent completed job's error + incidents for the
        web banner to keep showing until the user dismisses it."""
        return {
            "running": _JOB_STATE.get("running", False),
            "error": _JOB_STATE.get("error"),
            "message": _JOB_STATE.get("message"),
            "incidents": list(_JOB_STATE.get("incidents") or []),
            "last_error_kind": _JOB_STATE.get("last_error_kind"),
            "last_error_hint": _JOB_STATE.get("last_error_hint"),
            "acknowledged": _JOB_STATE.get("acknowledged", False),
            "finished_at": _JOB_STATE.get("finished_at"),
        }

    @app.post("/api/job/events/clear")
    def api_job_events_clear():
        """Clear the in-memory activity event stream and reset event counts."""
        _JOB_STATE["events"] = []
        _JOB_STATE["incidents"] = []
        _JOB_STATE["last_error_kind"] = None
        _JOB_STATE["last_error_hint"] = None
        _JOB_STATE["acknowledged"] = True
        return {"cleared": True, "total": 0}

    @app.post("/api/job/ack")
    def api_job_ack():
        """Mark the current error banner as acknowledged so it stops showing."""
        _JOB_STATE["acknowledged"] = True
        _JOB_STATE["error"] = None
        _JOB_STATE["last_error_kind"] = None
        _JOB_STATE["last_error_hint"] = None
        return {"acknowledged": True}

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

    @app.post("/api/leads/bulk-status")
    def api_bulk_status(body: "BulkStatusUpdate"):
        if body.status not in (db.APPROVED, db.REJECTED):
            raise HTTPException(400, "status must be 'approved' or 'rejected'")
        if not body.ids:
            return {"updated": 0, "skipped": 0}
        updated = 0
        skipped = 0
        for lead_id in body.ids:
            lead = db.get_lead(lead_id)
            if not lead:
                skipped += 1
                continue
            if lead.get("status") == body.status:
                skipped += 1
                continue
            db.set_status(lead_id, body.status)
            updated += 1
        return {"updated": updated, "skipped": skipped, "status": body.status}

    @app.get("/api/leads/count")
    def api_leads_count(
        status: str | None = None,
        min_score: int | None = None,
        target_product: str | None = None,
    ):
        """Lightweight count endpoint so the new leads table can show
        'Showing N of M' without re-rendering the page on every filter change."""
        return {"count": len(db.list_leads(
            status=status,
            min_score=min_score,
            target_product=target_product,
            limit=1000,
        ))}

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
