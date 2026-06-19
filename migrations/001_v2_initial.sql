-- ============================================================
-- CNVRTED V2 — Initial Migration
-- Run this in Supabase SQL Editor (new project)
-- ============================================================

-- Enable pgvector extension for ICP matching
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- GROUP 1: Users & Profiles
-- ============================================================

CREATE TABLE users (
  id                uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
  email             text          UNIQUE NOT NULL,
  created_at        timestamptz   DEFAULT now(),
  last_active_at    timestamptz   DEFAULT now(),
  subscription      text          DEFAULT 'beta' CHECK (subscription IN ('beta','active','churned'))
);

CREATE TABLE user_profiles (
  id                  uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             uuid          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name                text          NOT NULL DEFAULT 'My Profile',
  website_url         text,
  linkedin_url        text,
  service_description text,
  target_description  text,
  user_context        text,         -- UserContext.md — what they sell, tone, clients
  icp_text            text,         -- ICP.md — industry, size, buyer title, triggers
  icp_vector          vector(1536), -- pgvector embedding for matching
  is_active           bool          DEFAULT true,
  created_at          timestamptz   DEFAULT now(),
  updated_at          timestamptz   DEFAULT now()
);

CREATE TABLE user_preferences (
  id                    uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id            uuid    NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
  signal_weights        jsonb   DEFAULT '{"funding":1.0,"hiring":1.0,"reddit":1.0,"buyer_intent":1.0,"news":1.0}',
  min_intent_score      float   DEFAULT 0.60,
  preferred_industries  text[]  DEFAULT '{}',
  avoided_industries    text[]  DEFAULT '{}',
  leads_per_day         int     DEFAULT 20,
  email_digest          bool    DEFAULT true,
  digest_time           time    DEFAULT '08:00',
  timezone              text    DEFAULT 'UTC',
  total_interactions    int     DEFAULT 0,
  updated_at            timestamptz DEFAULT now()
);

-- ============================================================
-- GROUP 2: Signals
-- ============================================================

CREATE TABLE signals (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_hash      text        UNIQUE NOT NULL, -- SHA256 dedup key
  signal_type      text        NOT NULL CHECK (signal_type IN ('funding','hiring','buyer_post','news','semantic')),
  company_name     text,
  company_url      text,
  company_domain   text,
  raw_text         text        NOT NULL,
  source_url       text,
  source_platform  text,       -- reddit/crunchbase/serper/exa/rss
  funding_amount   numeric,
  funding_round    text,
  job_title        text,
  signal_date      timestamptz,
  ingested_at      timestamptz DEFAULT now(),
  status           text        DEFAULT 'pending' CHECK (status IN ('pending','processing','processed','failed'))
);

CREATE TABLE seen_signals (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id   uuid        NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
  signal_hash  text        NOT NULL,
  seen_at      timestamptz DEFAULT now(),
  action       text        DEFAULT 'viewed' CHECK (action IN ('viewed','saved','dismissed')),
  UNIQUE (profile_id, signal_hash)
);

-- ============================================================
-- GROUP 3: Leads
-- ============================================================

CREATE TABLE leads (
  id               uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id       uuid    NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
  signal_id        uuid    REFERENCES signals(id),
  company_name     text,
  company_url      text,
  company_domain   text,
  signal_type      text,
  why_flagged      text,
  intent_score     float,
  decision_maker   text,
  title            text,
  email            text,
  phone            text,
  linkedin_url     text,
  outreach_line    text,
  source_url       text,
  signal_date      timestamptz,
  list_date        date    DEFAULT CURRENT_DATE,
  status           text    DEFAULT 'new' CHECK (status IN ('new','viewed','saved','dismissed')),
  created_at       timestamptz DEFAULT now()
);

-- ============================================================
-- GROUP 4: Enrichment Cache
-- ============================================================

CREATE TABLE enrichment_cache (
  id               uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  company_domain   text    UNIQUE NOT NULL,
  company_name     text,
  decision_maker   text,
  title            text,
  email            text,
  phone            text,
  linkedin_url     text,
  source           text,   -- hunter/fullenrich/apollo
  enriched_at      timestamptz DEFAULT now(),
  expires_at       timestamptz DEFAULT now() + interval '30 days'
);

-- ============================================================
-- GROUP 5: Operations
-- ============================================================

CREATE TABLE agent_runs (
  id                 uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_name         text    NOT NULL,
  started_at         timestamptz DEFAULT now(),
  completed_at       timestamptz,
  status             text    DEFAULT 'running' CHECK (status IN ('running','completed','failed')),
  signals_found      int     DEFAULT 0,
  signals_processed  int     DEFAULT 0,
  signals_discarded  int     DEFAULT 0,
  error_message      text,
  metadata           jsonb   DEFAULT '{}'
);

CREATE TABLE daily_lists (
  id              uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id      uuid    NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
  list_date       date    NOT NULL DEFAULT CURRENT_DATE,
  lead_count      int     DEFAULT 0,
  email_sent      bool    DEFAULT false,
  email_sent_at   timestamptz,
  created_at      timestamptz DEFAULT now(),
  UNIQUE (profile_id, list_date)
);

CREATE TABLE api_logs (
  id               uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  service          text    NOT NULL,
  endpoint         text,
  status_code      int,
  success          bool,
  error_message    text,
  response_time_ms int,
  called_at        timestamptz DEFAULT now()
);

-- ============================================================
-- GROUP 6: The Static Moat
-- ============================================================

CREATE TABLE companies (
  id                uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  domain            text    UNIQUE NOT NULL,
  name              text,
  website_url       text,
  linkedin_url      text,
  industry          text,
  sub_industry      text,
  company_size      text,
  estimated_revenue text,
  founded_year      int,
  headquarters      text,
  description       text,
  technologies      text[]  DEFAULT '{}',
  last_enriched_at  timestamptz,
  first_seen_at     timestamptz DEFAULT now(),
  times_appeared    int     DEFAULT 1
);

CREATE TABLE decision_makers (
  id                uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id        uuid    REFERENCES companies(id),
  full_name         text,
  title             text,
  seniority         text    CHECK (seniority IN ('c_level','vp','director','manager','other')),
  department        text,
  email             text,
  phone             text,
  linkedin_url      text,
  twitter_url       text,
  location          text,
  enrichment_source text,
  first_seen_at     timestamptz DEFAULT now(),
  last_verified_at  timestamptz,
  confidence_score  float   DEFAULT 0.0
);

CREATE TABLE company_signals_history (
  id             uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id     uuid    REFERENCES companies(id),
  signal_type    text,
  signal_detail  text,
  signal_date    timestamptz,
  source_url     text,
  recorded_at    timestamptz DEFAULT now()
);

CREATE TABLE lead_authors (
  id               uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  platform         text    NOT NULL,
  platform_user_id text,
  username         text,
  display_name     text,
  profile_url      text,
  bio              text,
  location         text,
  follower_count   int     DEFAULT 0,
  company          text,
  title            text,
  first_seen_at    timestamptz DEFAULT now(),
  post_count       int     DEFAULT 1,
  intent_history   jsonb   DEFAULT '[]'
);

CREATE TABLE raw_posts (
  id            uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  post_hash     text    UNIQUE NOT NULL,
  platform      text    NOT NULL,
  author_id     uuid    REFERENCES lead_authors(id),
  company_id    uuid    REFERENCES companies(id),
  raw_text      text    NOT NULL,
  post_url      text,
  posted_at     timestamptz,
  ingested_at   timestamptz DEFAULT now(),
  intent_score  float,
  signal_type   text,
  used_in_lead  bool    DEFAULT false
);

-- ============================================================
-- Indexes for performance
-- ============================================================

CREATE INDEX idx_user_profiles_user_id     ON user_profiles(user_id);
CREATE INDEX idx_user_profiles_icp_vector  ON user_profiles USING ivfflat (icp_vector vector_cosine_ops);
CREATE INDEX idx_signals_hash              ON signals(signal_hash);
CREATE INDEX idx_signals_status            ON signals(status);
CREATE INDEX idx_signals_type              ON signals(signal_type);
CREATE INDEX idx_seen_signals_profile      ON seen_signals(profile_id);
CREATE INDEX idx_leads_profile_id          ON leads(profile_id);
CREATE INDEX idx_leads_list_date           ON leads(list_date);
CREATE INDEX idx_enrichment_domain         ON enrichment_cache(company_domain);
CREATE INDEX idx_companies_domain          ON companies(domain);
CREATE INDEX idx_agent_runs_agent          ON agent_runs(agent_name);
CREATE INDEX idx_raw_posts_hash            ON raw_posts(post_hash);

-- ============================================================
-- pgvector matching function
-- Used by matching engine: finds profiles that care about a signal
-- ============================================================

CREATE OR REPLACE FUNCTION match_profiles(
  query_vector    vector(1536),
  match_threshold float DEFAULT 0.70,
  match_count     int   DEFAULT 100
)
RETURNS TABLE (
  profile_id  uuid,
  user_id     uuid,
  similarity  float
)
LANGUAGE sql STABLE
AS $$
  SELECT
    id          AS profile_id,
    user_id,
    1 - (icp_vector <=> query_vector) AS similarity
  FROM user_profiles
  WHERE icp_vector IS NOT NULL
    AND is_active = true
    AND 1 - (icp_vector <=> query_vector) >= match_threshold
  ORDER BY icp_vector <=> query_vector
  LIMIT match_count;
$$;
