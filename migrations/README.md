# Database migrations

These `.sql` files build the cnvrted V2 schema from an empty database, **in order**.
They are applied **manually** in the Supabase SQL editor (nothing in the app runs them
automatically). A high-level map of every table is in [`../SCHEMA.md`](../SCHEMA.md).

## To rebuild the database from scratch

1. Create a new Supabase project (Postgres).
2. Open **SQL Editor**.
3. Run each file **in numeric order, `001` → `014`**, one at a time.
   All files are idempotent (`create table if not exists`, `add column if not exists`),
   so re-running a file is safe.
4. Set the backend env vars (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, …) to point at it.

That reproduces the full schema: tables, indexes, the pgvector extension, the
`match_profiles()` function, and row-level security.

## Order & history

| # | file | adds |
|---|---|---|
| 001 | `001_v2_initial.sql` | pgvector + core tables (users, user_profiles, signals, leads, companies…) + `match_profiles()` |
| 002 | `002_existing_clients.sql` | `existing_clients` |
| 003 | `003_search_profile.sql` | `user_profiles.search_profile` jsonb |
| 004 | `004_query_performance.sql` | `query_performance` |
| 005 | `005_watchlist.sql` | `watchlist_companies` |
| 006 | `006_watchlist_contacts.sql` | watchlist contact columns |
| 007 | `007_watchlist_feedback.sql` | watchlist `feedback` |
| 008 | `008_competitors.sql` | `competitors` |
| 009 | `009_watchlist_proof.sql` | watchlist proof/freshness columns |
| 010 | `010_auth.sql` | `users` password/username + `scan_runs` |
| 011 | `011_admin_costs.sql` | `api_usage` + `provider_balances` |
| 012 | `012_mail_items.sql` | `mail_items` |
| 013 | `013_pipeline_items.sql` | `pipeline_items` (+ RLS on that table) |
| 014 | `014_enable_rls.sql` | enables RLS on all remaining tables |

> **Renumbering note (2026-07):** `010`–`013` were previously in a second folder,
> `backend/migrations/`, numbered `002`–`005`, which collided with the root `002`–`005`
> above. They were moved here and renumbered chronologically (they are the later,
> July auth-era migrations) so the whole schema reads as one ordered sequence. Their SQL
> content is unchanged; git history is preserved via `git mv`.

> **`014_enable_rls.sql`** reconstructs a step that was originally run by hand in Supabase
> (enabling row-level security across all tables). It is idempotent and included so a
> from-scratch rebuild matches production. The backend uses the service-role key, which
> **bypasses RLS**, so enabling it never breaks the app — it's a deny-all backstop against
> direct/anonymous access.
