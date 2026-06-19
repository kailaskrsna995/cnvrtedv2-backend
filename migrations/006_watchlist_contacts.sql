-- Cache the decision-maker contact on each watchlist company (cold list).
ALTER TABLE watchlist_companies ADD COLUMN IF NOT EXISTS contact_name TEXT;
ALTER TABLE watchlist_companies ADD COLUMN IF NOT EXISTS contact_title TEXT;
ALTER TABLE watchlist_companies ADD COLUMN IF NOT EXISTS contact_linkedin TEXT;
ALTER TABLE watchlist_companies ADD COLUMN IF NOT EXISTS contact_checked_at TIMESTAMPTZ;
