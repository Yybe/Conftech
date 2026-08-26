"""database.py - Supabase persistence layer for Conftech.

Centralizes all access to the `leads` and `outreach_drafts` tables, plus the
schema bootstrap (init_schema). Uses the SERVICE ROLE key so agents can
read/write regardless of RLS policies (backend-only credential).

Status flow:
    raw -> researched -> pending_review -> approved -> drafted
                         pending_review -> rejected
"""

from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# --- Status constants -------------------------------------------------------
RAW = "raw"
RESEARCHED = "researched"
PENDING_REVIEW = "pending_review"
APPROVED = "approved"
REJECTED = "rejected"
DRAFTED = "drafted"
ERROR = "error"

ALL_STATUSES = (RAW, RESEARCHED, PENDING_REVIEW, APPROVED, REJECTED, DRAFTED, ERROR)

SCHEMA_SQL = """
-- Conftech schema (idempotent)
create extension if not exists "pgcrypto";

create table if not exists public.leads (
    id               uuid primary key default gen_random_uuid(),
    company_name     text not null,
    website          text,
    target_product   text,
    buyer_persona    text,
    matches_persona  boolean,
    tech_stack       jsonb not null default '[]'::jsonb,
    contact_email    text,
    fit_score        integer,
    confidence_score integer,
    reasoning        text,
    status           text not null default 'pending_review',
    source           text,
    scraped_data     jsonb not null default '{}'::jsonb,
    company_profile  jsonb not null default '{}'::jsonb,
    created_at       timestamptz not null default now()
);

-- Idempotent upgrade for databases created before these columns existed.
alter table public.leads add column if not exists confidence_score integer;
alter table public.leads add column if not exists company_profile jsonb not null default '{}'::jsonb;
alter table public.leads add column if not exists buyer_persona text;
alter table public.leads add column if not exists matches_persona boolean;

create index if not exists leads_status_idx on public.leads (status);

create table if not exists public.outreach_drafts (
    id         uuid primary key default gen_random_uuid(),
    lead_id    uuid not null references public.leads (id) on delete cascade,
    subject    text,
    email_body text not null,
    created_at timestamptz not null default now()
);

create index if not exists outreach_drafts_lead_idx on public.outreach_drafts (lead_id);
"""

_client: Client | None = None
_tables_verified: bool = False


class TableNotReadyError(RuntimeError):
    """Raised when the Supabase tables do not exist yet (PGRST205)."""


def get_client() -> Client:
    """Lazily create the Supabase client (service role).

    Also probes for the `leads` table once so every DB operation fails with a
    friendly, actionable message when the schema hasn't been created yet."""
    global _client, _tables_verified
    if _client is None:
        url = os.getenv("SUPABASE_URL", "").rstrip("/")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing. "
                "Copy .env.example to .env and fill in your keys."
            )
        _client = create_client(url, key)

    if not _tables_verified:
        try:
            _client.table("leads").select("id").limit(1).execute()
            _tables_verified = True
        except Exception as exc:  # noqa: BLE001 - inspect, then re-raise
            if getattr(exc, "code", "") == "PGRST205" or "Could not find the table" in str(exc):
                raise TableNotReadyError(
                    "Supabase tables do not exist yet. Create them with:\n"
                    "  python app.py init-db\n"
                    "(or paste schema.sql into Dashboard -> SQL Editor -> Run)"
                ) from exc
            raise
    return _client


# --- Schema bootstrap -------------------------------------------------------

def _project_ref(url: str) -> str | None:
    """Extract the project ref from https://<ref>.supabase.co, or None."""
    import re as _re

    m = _re.match(r"https?://([a-z0-9]+)\.supabase\.co", url or "")
    return m.group(1) if m else None


def _init_schema_via_management_api() -> bool:
    """Path 1: Management API, requires SUPABASE_ACCESS_TOKEN (sb_pat_...)
    created in Dashboard -> Account -> Access Tokens."""
    token = os.getenv("SUPABASE_ACCESS_TOKEN", "").strip()
    ref = _project_ref(os.getenv("SUPABASE_URL", ""))
    if not token or not ref:
        return False
    try:
        resp = requests.post(
            f"https://api.supabase.com/v1/projects/{ref}/database/query",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"query": SCHEMA_SQL},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            print("[database] Schema ready via Management API (tables: leads, outreach_drafts).")
            return True
        print(f"[database] Management API failed ({resp.status_code}): {resp.text[:200]}")
    except requests.RequestException as exc:
        print(f"[database] Management API error: {exc}")
    return False


def _init_schema_via_pg_query() -> bool:
    """Path 2: HTTP /pg/query endpoint with the service-role key (legacy,
    disabled on many newer projects)."""
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        return False
    try:
        resp = requests.post(
            f"{url}/pg/query",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"query": SCHEMA_SQL},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            print("[database] Schema ready (tables: leads, outreach_drafts).")
            return True
        print(f"[database] /pg/query unavailable ({resp.status_code}).")
    except requests.RequestException as exc:
        print(f"[database] /pg/query error: {exc}")
    return False


def init_schema() -> None:
    """Create tables. Tries, in order: Management API (SUPABASE_ACCESS_TOKEN),
    the HTTP /pg/query endpoint, then prints fallback instructions."""
    if _init_schema_via_management_api() or _init_schema_via_pg_query():
        return

    print(
        "[database] Could not run DDL automatically. Options:\n"
        "  1. Dashboard -> SQL Editor -> paste schema.sql -> Run (fastest).\n"
        "  2. Create an Access Token (Dashboard -> Account -> Access Tokens),\n"
        "     put it in .env as SUPABASE_ACCESS_TOKEN, and rerun `python app.py init-db`.\n"
        "  3. Complete `opencode mcp auth supabase` and I can create it via MCP.\n"
        "SQL to run:\n" + SCHEMA_SQL
    )


# --- Leads CRUD -------------------------------------------------------------

def insert_lead(
    company_name: str,
    website: str | None,
    tech_stack: list | None = None,
    source: str = "theirstack",
    target_product: str | None = None,
    contact_email: str | None = None,
    company_profile: dict | None = None,
    buyer_persona: str | None = None,
    matches_persona: bool | None = None,
) -> dict[str, Any]:
    """Insert a lead; if a lead with the same website exists, return it unchanged
    (dedupe). Returns the lead row dict.

    `matches_persona` is the cheap overlap check the prospector runs after
    the search: True if the lead's tech_stack / industry / employee count
    agrees with the buyer's persona, False if it doesn't. The UI shows a
    "BP" chip when True so the reviewer can see which leads were persona-
    matched vs which were broader-search fallback finds.

    NOTE: buyer_persona / matches_persona are also written into the
    company_profile JSONB so the data survives even when the dedicated
    columns haven't been added by the schema migration yet. The DB columns
    are added by the SCHEMA_SQL in this module when `init_schema()` can
    reach the Supabase Management API or `/pg/query`; on projects where
    those endpoints are disabled, the persona still persists via the JSONB
    fallback so the scorer and UI can read it back.
    """
    client = get_client()
    if website:
        existing = (
            client.table("leads").select("id").eq("website", website).limit(1).execute()
        )
        if existing.data:
            return existing.data[0]

    row: dict[str, Any] = {
        "company_name": company_name,
        "website": website,
        "tech_stack": tech_stack or [],
        "source": source,
        "status": RAW,
        "target_product": target_product,
    }
    if company_profile or buyer_persona or matches_persona is not None:
        # Merge persona / matches_persona into the company_profile JSONB
        # so the data survives even when the dedicated columns haven't
        # been added by the schema migration yet.
        profile = dict(company_profile or {})
        if buyer_persona:
            profile["buyer_persona"] = buyer_persona
        if matches_persona is not None:
            profile["matches_persona"] = matches_persona
        row["company_profile"] = profile
    if contact_email:
        row["contact_email"] = contact_email

    # Try to write the dedicated columns too. If the migration hasn't run
    # yet the insert will fail with a "column not found" error (PostgREST
    # reports this as either 42703 column-doesn't-exist or PGRST204
    # schema-cache miss depending on which layer rejected it); we retry
    # without those keys in either case. The persona still lands in
    # `company_profile.buyer_persona` so the scorer can read it back.
    try:
        if buyer_persona:
            row["buyer_persona"] = buyer_persona
        if matches_persona is not None:
            row["matches_persona"] = matches_persona
        res = client.table("leads").insert(row).execute()
        return res.data[0]
    except Exception as exc:
        msg = str(exc)
        col_missing_persona = (
            ("42703" in msg and "buyer_persona" in msg)
            or ("PGRST204" in msg and "buyer_persona" in msg)
            or ("Could not find the 'buyer_persona' column" in msg)
        )
        col_missing_matches = (
            ("42703" in msg and "matches_persona" in msg)
            or ("PGRST204" in msg and "matches_persona" in msg)
            or ("Could not find the 'matches_persona' column" in msg)
        )
        if col_missing_persona or col_missing_matches:
            row.pop("buyer_persona", None)
            row.pop("matches_persona", None)
            res = client.table("leads").insert(row).execute()
            return res.data[0]
        raise


def find_lead_by_website(website: str) -> dict[str, Any] | None:
    """Look up an existing lead by exact website URL. Returns the full row
    (so callers can see the current status) or None if not present.

    Used by the prospector to count NEW vs already-known leads honestly."""
    if not website:
        return None
    res = (
        get_client()
        .table("leads")
        .select("*")
        .eq("website", website)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_lead(lead_id: str) -> dict[str, Any] | None:
    res = get_client().table("leads").select("*").eq("id", lead_id).limit(1).execute()
    return res.data[0] if res.data else None


def get_leads_by_status(status: str, limit: int = 50) -> list[dict[str, Any]]:
    res = (
        get_client()
        .table("leads")
        .select("*")
        .eq("status", status)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(res.data)


def list_leads(
    status: str | None = None,
    min_score: int | None = None,
    target_product: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List leads with optional status / fit-score / target-product filters."""
    query = get_client().table("leads").select("*").order("created_at", desc=True)
    if status:
        query = query.eq("status", status)
    if min_score is not None:
        query = query.gte("fit_score", min_score)
    if target_product:
        query = query.eq("target_product", target_product)
    res = query.limit(limit).execute()
    return list(res.data)


def list_target_products() -> dict[str, int]:
    """Distinct target products with a lead count each, most-leads first."""
    res = (
        get_client()
        .table("leads")
        .select("target_product")
        .neq("target_product", "")
        .execute()
    )
    counts: dict[str, int] = {}
    for row in res.data or []:
        name = row.get("target_product") or "Untagged"
        counts[name] = counts.get(name, 0) + 1
    return dict(
        sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    )


def update_lead(lead_id: str, **fields: Any) -> dict[str, Any] | None:
    """Update arbitrary columns; silently ignores unknown keys."""
    allowed = {
        "company_name", "website", "target_product", "buyer_persona", "matches_persona",
        "tech_stack", "contact_email",
        "fit_score", "confidence_score", "reasoning", "status", "source",
        "scraped_data", "company_profile",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return get_lead(lead_id)
    res = get_client().table("leads").update(payload).eq("id", lead_id).execute()
    return res.data[0] if res.data else None


def set_status(lead_id: str, status: str) -> None:
    if status not in ALL_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    update_lead(lead_id, status=status)


# --- Outreach drafts --------------------------------------------------------

def insert_draft(lead_id: str, subject: str, email_body: str) -> dict[str, Any]:
    """Insert one draft per lead (existing draft for the lead is replaced)."""
    client = get_client()
    client.table("outreach_drafts").delete().eq("lead_id", lead_id).execute()
    res = (
        client.table("outreach_drafts")
        .insert({"lead_id": lead_id, "subject": subject, "email_body": email_body})
        .execute()
    )
    return res.data[0]


def list_drafts(limit: int = 50) -> list[dict[str, Any]]:
    res = (
        get_client()
        .table("outreach_drafts")
        .select("*, leads(company_name, website)")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(res.data)


def get_draft_for_lead(lead_id: str) -> dict[str, Any] | None:
    res = (
        get_client()
        .table("outreach_drafts")
        .select("*")
        .eq("lead_id", lead_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def delete_draft(draft_id: str) -> None:
    get_client().table("outreach_drafts").delete().eq("id", draft_id).execute()


def count_by_status() -> dict[str, int]:
    """Per-status counts via PostgREST (no SQL endpoint dependency)."""
    counts: dict[str, int] = {}
    for status in ALL_STATUSES:
        res = (
            get_client()
            .table("leads")
            .select("id", count="exact")
            .eq("status", status)
            .limit(1)
            .execute()
        )
        counts[status] = res.count if hasattr(res, "count") else 0
    return counts
