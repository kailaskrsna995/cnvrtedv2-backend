-- Proof + freshness for watchlist companies.
-- A company earns its cold-list spot only with a recent real signal (proof).
-- Dormant/defunct companies (no recent activity) get filtered out.
ALTER TABLE watchlist_companies ADD COLUMN IF NOT EXISTS proof_url TEXT;
ALTER TABLE watchlist_companies ADD COLUMN IF NOT EXISTS proof_summary TEXT;
ALTER TABLE watchlist_companies ADD COLUMN IF NOT EXISTS activity_checked_at TIMESTAMPTZ;
ALTER TABLE watchlist_companies ADD COLUMN IF NOT EXISTS is_active BOOLEAN;  -- null=unchecked, true=recent activity, false=dormant
