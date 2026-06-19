-- Existing clients blocklist — leads matching these are excluded from results
CREATE TABLE IF NOT EXISTS existing_clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    company_name TEXT,
    company_domain TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT existing_clients_profile_name_unique UNIQUE (profile_id, company_name),
    CONSTRAINT existing_clients_profile_domain_unique UNIQUE (profile_id, company_domain)
);

CREATE INDEX IF NOT EXISTS existing_clients_profile_idx ON existing_clients(profile_id);
