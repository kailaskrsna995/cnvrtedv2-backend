-- 014_enable_rls.sql — enable Row-Level Security on all tables.
-- Reconstructs a step originally run by hand in Supabase, so a from-scratch rebuild
-- matches production. Idempotent: enabling RLS that is already on is a no-op.
--
-- IMPORTANT: the backend connects with the Supabase SERVICE-ROLE key, which BYPASSES
-- RLS. So this is a deny-all backstop against direct / anonymous (anon-key) access —
-- it does NOT break the application. No per-row policies are defined (none are needed
-- until a client/anon key or Supabase Realtime is used directly from the browser).

alter table users                    enable row level security;
alter table user_profiles            enable row level security;
alter table user_preferences         enable row level security;
alter table signals                  enable row level security;
alter table seen_signals             enable row level security;
alter table leads                    enable row level security;
alter table enrichment_cache         enable row level security;
alter table agent_runs               enable row level security;
alter table daily_lists              enable row level security;
alter table api_logs                 enable row level security;
alter table companies                enable row level security;
alter table decision_makers          enable row level security;
alter table company_signals_history  enable row level security;
alter table lead_authors             enable row level security;
alter table raw_posts                enable row level security;
alter table existing_clients         enable row level security;
alter table query_performance        enable row level security;
alter table watchlist_companies      enable row level security;
alter table competitors              enable row level security;
alter table scan_runs                enable row level security;
alter table api_usage                enable row level security;
alter table provider_balances        enable row level security;
alter table mail_items               enable row level security;
alter table pipeline_items           enable row level security;
