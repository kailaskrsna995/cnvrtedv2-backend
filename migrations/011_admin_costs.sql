-- 003_admin_costs.sql — cost tracking + provider balances for the admin dashboard.
-- Run in the Supabase SQL editor. Additive & idempotent.

-- Aggregated API spend (flushed from the in-memory accumulator per scan).
create table if not exists api_usage (
    id           uuid primary key default gen_random_uuid(),
    provider     text not null,               -- anthropic | openai | apollo | serper | exa | ...
    model        text,
    input_units  bigint default 0,            -- tokens (llm) or requests/reveals
    output_units bigint default 0,
    cost_usd     numeric(14,6) not null default 0,
    meta         jsonb,
    created_at   timestamptz not null default now()
);
create index if not exists idx_api_usage_created  on api_usage (created_at);
create index if not exists idx_api_usage_provider on api_usage (provider);

-- Per-provider balances (metered) + flat monthly costs (fixed infra). Admins edit these.
create table if not exists provider_balances (
    provider       text primary key,           -- 'anthropic', 'railway', ...
    is_fixed       boolean not null default false,  -- true = flat monthly infra (Railway/Vercel/Supabase)
    balance_usd    numeric(14,2) default 0,     -- last-entered top-up balance (metered providers)
    monthly_usd    numeric(14,2) default 0,     -- flat monthly cost (fixed providers)
    balance_set_at timestamptz default now(),   -- spend AFTER this time counts against the balance
    note           text,
    updated_at     timestamptz not null default now()
);
