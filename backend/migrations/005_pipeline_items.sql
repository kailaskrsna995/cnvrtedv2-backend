-- 005_pipeline_items.sql
-- The TRACK backbone: one durable row per deal. This is how cnvrted "remembers" a deal
-- across reloads/sessions/devices (Postgres on disk), unlike scan leads which live in
-- ephemeral in-memory state. Each card is the CENTRAL RECORD every capability writes to
-- (stage changes, emails, notes, and later meeting summaries) via the activity log.

create table if not exists pipeline_items (
  id          uuid primary key default gen_random_uuid(),
  profile_id  uuid not null,                       -- which workspace/seller owns this deal
  lead_key    text not null,                       -- stable id of the lead (company/source)
  company     text,                                -- denormalized for display
  lead        jsonb,                               -- SNAPSHOT of the lead → card stands alone
                                                    -- even if the scan cache clears
  stage       text not null default 'new',         -- new|contacted|replied|meeting|in_talks|won|lost
  value       numeric,                             -- optional deal-value estimate
  next_step   text,                                -- what to do next (free text)
  activity    jsonb not null default '[]'::jsonb,  -- typed event log:
                                                    -- [{type, text, at, meta?}] — holds stage
                                                    -- changes, emails, notes, future meeting summaries
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (profile_id, lead_key)                    -- one card per lead → "add" is idempotent
);

create index if not exists pipeline_items_profile_idx on pipeline_items (profile_id);

-- Match the deny-all RLS posture of every other table (backend uses the service-role key,
-- which BYPASSES RLS, so this is a non-breaking backstop against direct/anon access).
alter table pipeline_items enable row level security;
