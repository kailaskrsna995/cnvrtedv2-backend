-- 002_auth.sql — real per-user auth + per-user daily scan cap.
-- Run this in the Supabase SQL editor (service role). Additive & idempotent —
-- it does NOT touch existing rows/data. Existing users get NULL password_hash
-- (they simply can't log in until they register/claim their email).

-- 1. credentials on the existing users table
alter table users add column if not exists password_hash text;
alter table users add column if not exists username text;

-- 2. durable per-user scan log → powers the "2 runs/day" cap (counts today's rows)
create table if not exists scan_runs (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references users(id) on delete cascade,
    profile_id uuid,
    created_at timestamptz not null default now()
);

create index if not exists idx_scan_runs_user_created
    on scan_runs (user_id, created_at);
