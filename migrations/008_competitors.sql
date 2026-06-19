-- Competitors per profile (from ICP research lookalikes).
-- Shown on their own page; excluded from the lead list.
CREATE TABLE IF NOT EXISTS competitors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT competitors_unique UNIQUE (profile_id, name)
);
CREATE INDEX IF NOT EXISTS competitors_profile_idx ON competitors(profile_id);
