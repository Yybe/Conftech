# Conftech — AI Intelligence Lead Platform

Automated lead generation and intelligence platform inspired by
[Nurix.ai](https://nurix.ai) and [Seamless.ai](https://seamless.ai). It identifies
high-potential customers for a **Target Product**, ranks them with a fit score, and
generates hyper-personalized outreach drafts — with a human approving every email
before it exists.

```
┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  Prospector │──▶│    Deep      │──▶│   Scorer     │──▶│  Ghostwriter │
│ TheirStack  │   │  Researcher  │   │  Gemma-4 via │   │  Gemma-4 via │
│   + Serper  │   │ ScrapeGraphAI│   │   OpenRouter │   │   OpenRouter │
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

## Status flow

```
raw → researched → pending_review → approved → drafted
                        └── rejected (human)
```

## Architecture

| Module | Role |
|---|---|
| `database.py` | Supabase persistence (service-role client), schema bootstrap, CRUD |
| `prospector.py` | TheirStack (tech-filtered company search) + Serper (email/LinkedIn enrichment) → `raw` |
| `researcher.py` | 3-tier web crawl → `scraped_data` → `researched`: **1)** ScrapeGraphAI (real browser rendering) **2)** BS4 + Gemma analysis **3)** BS4 raw |
| `scorer.py` | Gemma-4 via OpenRouter: `fit_score` (1–100) + reasoning → `pending_review` |
| `ghostwriter.py` | Gemma-4 cold email for **approved** leads → `outreach_drafts` → `drafted` |
| `webapp.py` | FastAPI web UI (dashboard / leads / drafts) + JSON API; agents run in BackgroundTasks |
| `app.py` | CLI + server; human `review_leads()`; `watch` trigger loop |
| `llm.py` | Shared OpenRouter helper + robust JSON parsing |
| `cache.py` | Disk JSON cache with TTL (TheirStack 14d, Serper 30d, pages 30d) |
| `scrapegraphai_compat.py` | Patches `ChatOllama` back into langchain-community 0.4+ (removed there) |
| `prompts/*.md` | All LLM prompts (never embedded in Python) |

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
python app.py serve --port 8000
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
  `contact_email`, `fit_score (int)`, `reasoning (text)`, `status` (default
  `pending_review`), `source`, `scraped_data (jsonb)`, `created_at`
- **outreach_drafts**: `id (uuid)`, `lead_id (fk -> leads, cascade)`, `subject`,
  `email_body`, `created_at`

Schema is created by `python app.py init-db`, which tries in order:
1. **Management API** — set `SUPABASE_ACCESS_TOKEN=sb_pat_...` in `.env` (Dashboard → Account → Access Tokens)
2. **HTTP `/pg/query`** — legacy endpoint (disabled on many newer projects)
3. Falls back to printing the SQL — paste `schema.sql` into the Dashboard → SQL Editor → Run

## Security notes

- All credentials live in `.env` (gitignored). **Never commit real keys.**
- The code uses the **service-role** key — backend-only; never ship it to a browser
  client. Add RLS policies if you expose data client-side.
- TheirStack requires `Authorization: Bearer <key>` — `X-API-KEY` returns 401.
- `google/gemma-2-9b-it:free` is deprecated (404). Default model is
  `google/gemma-4-26b-a4b-it:free`, overridable with `LLM_MODEL` in `.env`.

## Roadmap

- [x] Web review UI (dashboard, approve/reject, draft previews) — `python app.py serve`
- [x] ScrapeGraphAI browser rendering for research (3-tier fallback)
- [ ] Email sending (Resend/SendGrid) + open/reply tracking
- [ ] Contact email enrichment (paid Serper tier or Hunter/Prospeo)
- [ ] Deeper scoring signals (job postings, hiring intent, funding events)
- [ ] RLS policies + anon-key client for a multi-user review UI
