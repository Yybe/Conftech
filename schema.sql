-- Conftech schema (idempotent). Run in Supabase Dashboard -> SQL Editor,
-- or `python app.py init-db`.

create extension if not exists "pgcrypto";

create table if not exists public.leads (
    id             uuid primary key default gen_random_uuid(),
    company_name   text not null,
    website        text,
    target_product text,
    tech_stack     jsonb not null default '[]'::jsonb,
    contact_email  text,
    fit_score      integer,
    reasoning      text,
    status         text not null default 'pending_review',
    source         text,
    scraped_data   jsonb not null default '{}'::jsonb,
    created_at     timestamptz not null default now()
);

create index if not exists leads_status_idx on public.leads (status);

create table if not exists public.outreach_drafts (
    id         uuid primary key default gen_random_uuid(),
    lead_id    uuid not null references public.leads (id) on delete cascade,
    subject    text,
    email_body text not null,
    created_at timestamptz not null default now()
);

create index if not exists outreach_drafts_lead_idx on public.outreach_drafts (lead_id);
