# cnvrted V2 — Database Schema

The database is **PostgreSQL (Supabase)** with the **pgvector** extension for ICP
similarity matching. The backend talks to it with the Supabase service-role key.

- **Source of truth = the live database.** The `migrations/` folder is the *recipe*
  that builds this schema from an empty database; the running Supabase project is the
  *result*. If the two ever disagree, the live DB wins — see
  [How to read the true schema](#how-to-read-the-true-live-schema) below.
- **To rebuild from scratch:** run `migrations/001` … `migrations/013` in order in the
  Supabase SQL editor. See `migrations/README.md`.

---

## The model at a glance

```
users ──1:N── user_profiles ──1:N── user_preferences
                    │                 leads · seen_signals · existing_clients
                    │                 watchlist_companies · competitors
                    │                 query_performance · daily_lists
                    │                 mail_items · pipeline_items
                    └── search_profile (jsonb: facets, dossier, signal_recipes)

signals ──1:N── leads              (a scored signal becomes a lead)
companies ──1:N── decision_makers · company_signals_history · raw_posts
lead_authors ──1:N── raw_posts

Auth/ops (not profile-scoped): scan_runs · api_usage · provider_balances
                               agent_runs · api_logs · enrichment_cache
```

Everything a *seller* owns hangs off **`user_profiles`** (one user can have several
profiles / workspaces). `profile_id` is the multi-tenant boundary — the app's ownership
checks (`assert_owner`) gate every profile-scoped row so one user never sees another's data.

---

## 1. Accounts & tenancy

### `users` — one login
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| email | text unique | |
| password_hash | text | bcrypt (added in `010_auth`) |
| username | text | added in `010_auth` |
| subscription | text | `beta` \| `active` \| `churned` |
| created_at, last_active_at | timestamptz | |

### `user_profiles` — one seller/workspace (the tenant boundary)
| column | type | notes |
|---|---|---|
| id | uuid pk | this is `profile_id` everywhere else |
| user_id | uuid → users | ON DELETE CASCADE |
| name, website_url, linkedin_url | text | |
| service_description, target_description, user_context | text | raw onboarding inputs |
| icp_text | text | generated ICP description |
| icp_vector | vector(1536) | OpenAI embedding, used by `match_profiles()` |
| search_profile | jsonb | **the brain** — search facets + `dossier` + `signal_recipes` (+ fingerprint) live as keys inside here (added in `003`) |
| is_active | bool | |

### `user_preferences` — per-profile tuning
Signal weights (jsonb), `min_intent_score`, preferred/avoided industries, digest settings.
One row per `profile_id`.

---

## 2. Signals → Leads (the core pipeline)

### `signals` — a raw buying signal an agent found
Funding raise, job post, buyer post, news item. Keyed by `signal_hash` (SHA-256 dedup).
Carries `signal_type`, company fields, `raw_text`, `source_url`, `source_platform`, status.

### `leads` — a signal that passed scoring, shown to the user
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| profile_id | uuid → user_profiles | owner |
| signal_id | uuid → signals | the evidence |
| company_name / _url / _domain | text | |
| why_flagged | text | the human explanation ("proof") |
| intent_score | float | scorer output |
| decision_maker, title, email, phone, linkedin_url | text | enrichment |
| outreach_line | text | (legacy; per-lead opener now removed) |
| status | text | `new` \| `viewed` \| `saved` \| `dismissed` |

### `seen_signals`
Which signals a profile has already been shown (`viewed`/`saved`/`dismissed`), so repeats
are suppressed. Unique on `(profile_id, signal_hash)`.

> **Note on drift:** finished scan results are also persisted to `lead_runs` (referenced in
> the app), while *in-progress* scans live in an in-memory job store. `leads` above is the
> original per-lead table; confirm against the live DB which one your current code writes to.

---

## 3. Search intelligence (per profile)

| table | purpose |
|---|---|
| `existing_clients` | blocklist — the seller's current clients, excluded from results |
| `competitors` | the seller's competitors (from ICP lookalike research), shown separately, excluded from leads |
| `watchlist_companies` | in-ICP "target list" companies, monitored for fresh triggers each run. Grew over `005→009` to carry cached contact (`contact_*`), user `feedback` (liked/disliked), and `proof_*` + `is_active` freshness |
| `query_performance` | per-query outcome log (runs / signals / leads-passed) so the query builder drops chronically dead queries |
| `daily_lists` | one row per profile per day — lead count + whether the digest email was sent |

---

## 4. The static "moat" (cross-profile knowledge)

Not scoped to a profile — a shared, growing knowledge base of companies & people.

| table | purpose |
|---|---|
| `companies` | enriched company records (industry, size, revenue, tech stack…), keyed by `domain` |
| `decision_makers` | people at those companies (name, title, seniority, contacts) |
| `company_signals_history` | timeline of signals seen per company (the seed of the "score-sheet over time" idea) |
| `lead_authors` | people who posted buyer-intent content (platform profile + `intent_history`) |
| `raw_posts` | raw scraped posts, linked to author + company |
| `enrichment_cache` | domain → contact cache with a 30-day `expires_at` (avoids re-paying enrichment APIs) |

---

## 5. Reach & Track (the dashboard pivot)

### `mail_items` — the Mail tab (added `012`)
One row per lead per profile the user has queued to email. Holds a `lead` jsonb **snapshot**
(so it survives even if the scan cache clears), `status` (`selected`/`sent`), `subject`, `body`.
Unique on `(profile_id, lead_key)`.

### `pipeline_items` — the deal pipeline / TRACK backbone (added `013`)
The durable record of a deal as it moves through stages.
| column | type | notes |
|---|---|---|
| profile_id | uuid | owner |
| lead_key | text | stable lead id; unique per profile → "add" is idempotent |
| company | text | denormalized for display |
| lead | jsonb | snapshot → card stands alone |
| stage | text | `new`→`contacted`→`replied`→`meeting`→`in_talks`→`won`/`lost` |
| value | numeric | optional deal value |
| next_step | text | free text |
| activity | jsonb | typed event log `[{type, text, at, meta?}]` — stage changes, emails, notes |

Has **row-level security enabled** in-migration (backend service-role key bypasses it; it's a
deny-all backstop against direct/anon access).

---

## 6. Auth, cost & ops

| table | purpose |
|---|---|
| `scan_runs` | durable log of every scan (user_id, profile_id, time) → powers the per-user scan cap (added `010`) |
| `api_usage` | aggregated third-party spend flushed per scan: provider, model, units, `cost_usd`, `meta` jsonb (added `011`) |
| `provider_balances` | per-provider balance / flat monthly infra cost, edited from the admin dashboard (added `011`) |
| `agent_runs` | per-agent run log (status, signals found/processed/discarded) |
| `api_logs` | per-external-call log (service, status, latency) |

---

## Functions

### `match_profiles(query_vector, match_threshold, match_count)`
pgvector cosine search — given a signal's embedding, returns the profiles whose `icp_vector`
is similar enough (`1 - (icp_vector <=> query_vector) >= threshold`), active only. This is how
a signal finds the sellers who'd care about it.

---

## Known gaps between migrations and the live DB (be honest in the audit)

1. **Row-level security** was enabled on ~all tables via a **manual SQL step that is not in a
   migration file** (only `013_pipeline_items` enables RLS in-file). Rebuilding purely from
   `migrations/` would leave RLS off on the other tables. See `migrations/014_enable_rls.sql`
   if/when we add it. *(Backend uses the service-role key which bypasses RLS regardless, so the
   app works either way — but the recipe is incomplete without it.)*
2. **`search_profile` is a jsonb blob**, not columns — `dossier`, `signal_recipes`,
   `signal_recipes_fp` and the search facets are keys inside it, so they won't show up as table
   columns in an ER diagram.
3. Some columns/tables may have been added directly in the dashboard over time. **Treat the live
   DB as authoritative** and regenerate this doc from it before any high-stakes review.

---

## How to read the true (live) schema

For a presentation, don't trust these files — pull the real thing:

1. **Supabase Dashboard → Database → Schema Visualizer** — an auto-generated ER diagram of the
   live tables and foreign keys. Best for a screenshot.
2. **SQL (paste into the SQL editor)** — every table and column, current:
   ```sql
   select table_name, column_name, data_type, is_nullable
   from information_schema.columns
   where table_schema = 'public'
   order by table_name, ordinal_position;
   ```
3. **`pg_dump --schema-only`** (using the project's connection string) — the exact DDL of the
   live database, which you can diff against `migrations/` to catch drift.
