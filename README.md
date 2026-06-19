# cnvrted V2

**Lead intelligence for client acquisition.** You describe what you sell; cnvrted finds companies that are *in-market right now* — via trigger events (funding, news, hiring) and stated intent (public buyer posts) — scores them against your ICP, and writes a personalized outreach opener for each.

> Built for sellers (agencies, studios, SaaS) doing outbound. The output is a ranked lead list with proof + a suggested first line, plus a monitored "Target List" of ideal-fit accounts.

---

## How it works (the pipeline)

```
User input (website + service)
  → Profile Agent (crawl + Claude ICP generation)
  → ICP Sharpener (researches your real clients → evidence-weighted ICP)
  → Embed (OpenAI) + Build Search Profile (delivery-model + facets)
  → Query Builder
  → 4 agents in parallel → shared queue:
       Funding · News · Buyer-Intent · Watchlist
  → Pre-filter → Vector gate (pgvector) → Scoring (Haiku+Sonnet)
  → Dedup + Threshold → Judge (Sonnet) → Outreach (Sonnet)
  → Dashboard
```

Full diagram + the **actual prompt for every agent** are in [`VC_ARCHITECTURE.md`](VC_ARCHITECTURE.md) and the [`prompts/`](prompts/) folder.

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI (Python), port 8001 |
| Frontend | Next.js + Tailwind, port 3000 |
| DB / vectors | Supabase (Postgres + pgvector) |
| LLM | Claude Sonnet 4.5 (ICP/judge/outreach) + Haiku 4.5 (extraction/scoring) |
| Embeddings | OpenAI `text-embedding-3-small` (1536-d) |
| Search | Serper (news + web), Exa (semantic), HN Algolia |
| Crawl | crawl4ai → Jina Reader fallback; trafilatura (article text) |

## Setup

```bash
# 1. Backend
cd backend
python -m venv venv
venv\Scripts\activate            # Windows  (source venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
copy ..\.env.example .env         # then fill in your keys (see below)

# 2. Frontend
cd ../frontend
npm install

# 3. Database — run the SQL in migrations/ (001 … 009) against your Supabase project
```

Fill `backend/.env` (or `.env` at repo root) from [`.env.example`](.env.example): Supabase, Anthropic, OpenAI, Serper, Exa keys at minimum.

## Run

```bash
# Backend (terminal 1)
cd backend
venv\Scripts\uvicorn.exe main:app --port 8001

# Frontend (terminal 2)
cd frontend
npm run dev
```

Open `http://localhost:3000/dashboard?profile_id=<id>` and hit **Run Now**.

## Repo layout

| Path | What |
|---|---|
| `backend/app/agents/` | the source agents (profile, funding, news, buyer_intent, watchlist) |
| `backend/app/pipeline/` | scoring, judge, outreach, matching, query builder |
| `backend/app/routes/` | API routes (`leads_v2.py` is the main run pipeline) |
| `backend/prompt_lab/` | harness to test ICP-generation prompts efficiently |
| `frontend/app/dashboard/` | the leads dashboard |
| `migrations/` | Supabase schema (run in order) |
| `prompts/` | every agent's verbatim prompt (for review/audit) |
| `VC_ARCHITECTURE.md` | full pipeline diagram + prompts + tech stack |

## Notes

- `.env` is gitignored — **never commit real keys**. Use `.env.example` as the template.
- Scoring runs async (Haiku primary, Sonnet re-scores borderline + judges). Concurrency capped at 5.
- Completed runs are cached to `backend/results_cache/` so a backend restart doesn't blank the dashboard.
