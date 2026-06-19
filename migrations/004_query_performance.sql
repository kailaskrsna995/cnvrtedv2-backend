-- Per-query outcome tracking — search gets smarter every run.
-- Logged after each scored run; query_builder drops chronic zero-lead queries.
CREATE TABLE IF NOT EXISTS query_performance (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    agent TEXT NOT NULL,                -- funding / news / buyer_intent
    query TEXT NOT NULL,
    runs INT DEFAULT 0,                 -- times this query was executed
    signals_queued INT DEFAULT 0,       -- signals it produced (post-extraction)
    leads_passed INT DEFAULT 0,         -- leads that passed scoring threshold
    last_run_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT query_perf_unique UNIQUE (profile_id, agent, query)
);

CREATE INDEX IF NOT EXISTS query_perf_profile_idx ON query_performance(profile_id, agent);
