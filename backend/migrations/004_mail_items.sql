-- 004_mail_items.sql — persistent mailing list per profile (survives sessions).
-- The Mail tab / send-list + sent status live here (was React state, lost on reload).
-- Run in the Supabase SQL editor. Additive & idempotent.

create table if not exists mail_items (
    id          uuid primary key default gen_random_uuid(),
    profile_id  uuid not null,
    lead_key    text not null,                       -- the lead's stable id (company/source) — unique per profile
    company     text,
    lead        jsonb,                               -- snapshot {id, company, contact, email, ...} for the Mail tab + composer
    status      text not null default 'selected',    -- 'selected' | 'sent'
    subject     text,
    body        text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    unique (profile_id, lead_key)                    -- one row per lead per profile
);

create index if not exists idx_mail_items_profile on mail_items (profile_id);
