# Conftech — AI Intelligence Lead Platform

Automated lead generation and intelligence platform inspired by
[Nurix.ai](https://nurix.ai) and [Seamless.ai](https://seamless.ai). It identifies
high-potential customers for a **Target Product**, ranks them with a fit score, and
generates hyper-personalized outreach drafts — with a human approving every email
before it exists.

```
┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  Prospector │──▶│    Deep      │──▶│   Scorer     │──▶│  Ghostwriter │
│ TheirStack  │   │  Researcher  │   │  DeepSeek V4 │   │  DeepSeek V4 │
│   + Serper  │   │ ScrapeGraphAI│   │  Flash→MiMo  │   │  Flash→MiMo  │
└─────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
      │                  │                  │                  │
      ▼                  ▼                  ▼                  ▼
   status=raw      status=researched   status=pending_     status=drafted
                                         review
                                              │
                                        human approves
                                              ▼
                                    outreach_drafts (emails)
```

LLM routing: every agent call tries a chain of free models on the OpenCode
Zen gateway (`https://opencode.ai/zen/v1`), in order:
**`mimo-v2.5-free` → `hy3-free` → `nemotron-3-ultra-free`**. On any failure
(5xx, rate limit, empty content, invalid JSON) the call automatically falls
through to the next model. Hidden reasoning is disabled
(`reasoning_effort: "none"` — these models are reasoning models and would
otherwise burn their token budget on `reasoning_content` before answering);
override with `LLM_MODEL` / `LLM_FALLBACK_MODEL` / `LLM_FALLBACK_MODEL_2` in
`.env`, re-enable reasoning with `LLM_REASONING_EFFORT=low|medium|high`, or
switch the gateway with `OPENROUTER_BASE_URL`.

> **Avoid these models right now:** `muse-spark-1.2-contributor-free` (returns
> 500), `deepseek-v4-flash-free` (currently "Model is unavailable"),
> `x-preview-f-free` (currently "Endpoint unavailable"), and the paid
> `grok-build-0.1` / `kimi-k2.5` (require a payment method on the OpenCode
> workspace — the free models above do not).

## Status flow

```
raw → researched → pending_review → approved → drafted
                        └── rejected (human)
```

## Architecture

| Module | Role |
|---|---|
| `database.py` | Supabase persistence (service-role client), schema bootstrap, CRUD |
| `prospector.py` | TheirStack (tech-filtered company search) + Serper (email/LinkedIn enrichment) → `raw`; **filters out poor cold-outreach targets** (public sector / defense / 10k+ employee enterprises / public listings) from public metadata; saves `company_profile` |
| `researcher.py` | 3-tier web crawl → `scraped_data` → `researched`: **1)** ScrapeGraphAI (real browser rendering) **2)** BS4 + LLM analysis **3)** BS4 raw. **Ranks contact emails** (decision-maker first), attaches page titles to every evidence item, and **detects explicit buying signals** in page text |
| `scorer.py` | DeepSeek V4 Flash → MiMo V2.5 Free via OpenCode Zen: `fit_score` (1–100) + reasoning + **deterministic `confidence_score`** + **top reasons** + evidence citations → `pending_review` |
| `ghostwriter.py` | DeepSeek V4 Flash → MiMo V2.5 Free cold email for **approved** leads → `outreach_drafts` → `drafted` |
| `webapp.py` | FastAPI web UI (dashboard / leads / drafts) + JSON API; agents run in BackgroundTasks |
| `app.py` | CLI + server; human `review_leads()`; `watch` trigger loop |
| `llm.py` | Shared LLM gateway helper (OpenCode Zen, primary → fallback model chain) + robust JSON parsing |
| `cache.py` | Disk JSON cache with TTL (TheirStack 14d, Serper 30d, pages 30d) |
| `scrapegraphai_compat.py` | Patches `ChatOllama` back into langchain-community 0.4+ (removed there) |
| `prompts/*.md` | All LLM prompts (never embedded in Python) |

## Lead quality features

- **Company filtering** — companies are classified from public TheirStack
  metadata (employee count/range, public listing, industry, description,
  tags, domain) before they enter the pipeline. Public-sector/defense domains
  and descriptions, and 10,000+ employee enterprises are excluded entirely;
  publicly-traded and government-adjacent companies are kept but penalized at
  scoring (`company_profile.segment` → `scorer._SEGMENT_PENALTY`). No company
  names are hardcoded.
- **Ranked contacts** — emails are ranked decision-maker first
  (`cto@`, `engineering@`, `infrastructure@`, `security@`, `devops@`, ...),
  then functional (`sales@`, `procurement@`, ...), then generic (`info@`,
  `support@`, `careers@`). Generics are stored but tagged low priority.
- **Confidence score** — deterministic 0–100 computed from measurable factors:
  pages crawled, evidence volume, share of evidence with verbatim quotes, tech
  detections, structured info depth, cross-page consistency, buying signals,
  and extractor tier. Never an LLM guess.
- **Top reasons** — a strongest-first "why this lead" list, each reason backed
  by source URL, verbatim quote, and page title, clickable in the UI.
- **Buying signals** — explicit phrases on the site (`cloud migration`,
  `legacy system`, `technical debt`, `modernization`, `hybrid cloud`, ...) are
  detected deterministically, stored as `buying_signal` evidence, and given
  significant positive weight in both LLM and fallback scoring.
- **Source links everywhere** — every insight rendered in the UI links to the
  page it came from, with quote + title. Invented citations are dropped by URL
  validation.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in your keys
python app.py init-db     # create leads + outreach_drafts tables
```

Optional — full web crawling with ScrapeGraphAI (falls back to plain HTML parsing
automatically if not installed):

```bash
pip install scrapegraphai playwright
playwright install chromium
```

> **Environment note (Windows + Python 3.10):** scrapegraphai pulls in numpy 2.x.
> Upgrade the older compiled stack it coexists with, or you'll hit
> `numpy.dtype size changed` / `cannot import name 'ChatOllama'` errors:
>
> ```bash
> pip install -U "scipy>=1.13" "scikit-learn>=1.4.2" pandas "langchain-community>=0.4"
> ```
>
> `scrapegraphai_compat.py` re-exports `ChatOllama` from `langchain_ollama`
> (removed from langchain-community 0.4+); it's applied automatically before any
> scrapegraphai import.

## Usage

```bash
# Full pipeline: prospect -> research -> score
python app.py pipeline --target "Cloud Migration Services" --limit 10

# Or step by step
python app.py prospect --target "Cloud Migration Services" --limit 10
python app.py research --limit 20
python app.py score --limit 20

# Human review: approve/reject pending leads
python app.py review
#   a3  -> approve lead #3 (Ghostwriter will draft an email)
#   r5  -> reject lead #5

# Draft emails for approved leads
python app.py ghostwrite

# Trigger: auto-draft whenever a lead is approved
python app.py watch --interval 60

# Web UI + REST API (open http://localhost:8000)
# IMPORTANT on Windows: run with the project's venv Python so the `ddgs`
# package actually loads. Using `py` resolves to the Anaconda install on
# this machine, which has no ddgs, and the server silently falls back to
# Serper-only - which then immediately hits the "Query pattern not allowed
# for free accounts" 400. Use `serve.bat` (one-liner wrapper) or invoke
# .venv\Scripts\python.exe directly:
serve.bat
#   - or -
.venv\Scripts\python.exe -m uvicorn webapp:build_app --factory --port 8000
```

## Web UI

`python app.py serve` launches a dark-mode dashboard (FastAPI + Jinja2 + Tailwind):

| Page | URL | What you can do |
|---|---|---|
| Dashboard | `/` | Status counts, recent leads/drafts, **Run pipeline** form |
| Leads | `/leads` | Filter by status / min fit score; open any lead |
| Lead detail | `/leads/{id}` | Approve / reject; view research data + reasoning |
| Drafts | `/drafts` | Preview emails, copy to clipboard, delete |

Approving a lead on the detail page auto-generates the outreach email in the
background (status `approved` → `drafted`).

REST API (for scripts / integration):

```bash
curl -X POST localhost:8000/api/pipeline -H "Content-Type: application/json" \
     -d '{"target_product":"Automated Payroll Software","limit":5}'
curl localhost:8000/api/leads?status=pending_review
curl -X POST localhost:8000/api/leads/<id>/status -H "Content-Type: application/json" -d '{"status":"approved"}'
curl localhost:8000/api/drafts
```

## Database

Supabase (project `mnjlrpkpldgpxdwtqdaj`, region ap-southeast-2).

- **leads**: `id (uuid)`, `company_name`, `website`, `target_product`, `tech_stack (jsonb)`,
  `contact_email`, `fit_score (int)`, `confidence_score (int)`, `reasoning (text)`, `status` (default
  `pending_review`), `source`, `scraped_data (jsonb)`, `company_profile (jsonb)`, `created_at`
- **outreach_drafts**: `id (uuid)`, `lead_id (fk -> leads, cascade)`, `subject`,
  `email_body`, `created_at`

Schema is created by `python app.py init-db` (re-run after an upgrade — the
`alter table ... add column if not exists` statements add the new columns to
existing databases), which tries in order:
1. **Management API** — set `SUPABASE_ACCESS_TOKEN=sb_pat_...` in `.env` (Dashboard → Account → Access Tokens)
2. **HTTP `/pg/query`** — legacy endpoint (disabled on many newer projects)
3. Falls back to printing the SQL — paste `schema.sql` into the Dashboard → SQL Editor → Run

## Security notes

- All credentials live in `.env` (gitignored). **Never commit real keys.**
- The code uses the **service-role** key — backend-only; never ship it to a browser
  client. Add RLS policies if you expose data client-side.
- TheirStack requires `Authorization: Bearer <key>` — `X-API-KEY` returns 401.
- LLM model chain: primary `deepseek-v4-flash-free` → fallback
  `mimo-v2.5-free` (OpenCode Zen gateway, reasoning disabled via
  `reasoning_effort: "none"`). Override with `LLM_MODEL` /
  `LLM_FALLBACK_MODEL` / `LLM_REASONING_EFFORT` in `.env`.

## Roadmap

- [x] Web review UI (dashboard, approve/reject, draft previews) — `python app.py serve`
- [x] ScrapeGraphAI browser rendering for research (3-tier fallback)
- [x] Lead quality: company filtering, ranked contacts, confidence score, top reasons, buying signals
- [ ] Email sending (Resend/SendGrid) + open/reply tracking
- [ ] Contact email enrichment (paid Serper tier or Hunter/Prospeo)
- [ ] Deeper scoring signals (job postings, hiring intent, funding events)
- [ ] RLS policies + anon-key client for a multi-user review UI
