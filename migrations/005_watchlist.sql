-- Per-profile company watchlist — in-ICP companies discovered once,
-- monitored for fresh trigger events every run.
CREATE TABLE IF NOT EXISTS watchlist_companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    company_name TEXT NOT NULL,
    company_domain TEXT,
    reason TEXT,                        -- why it's in-ICP (e.g. "competitor of Pocket FM")
    source TEXT DEFAULT 'claude',       -- claude / agent_discovery / manual
    last_checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT watchlist_unique UNIQUE (profile_id, company_name)
);

CREATE INDEX IF NOT EXISTS watchlist_profile_idx ON watchlist_companies(profile_id);
