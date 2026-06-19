-- User feedback on watchlist companies (grows-with-user loop).
-- liked = keep/prioritize; disliked = exclude from monitoring + cold list.
ALTER TABLE watchlist_companies ADD COLUMN IF NOT EXISTS feedback TEXT;  -- null | 'liked' | 'disliked'
