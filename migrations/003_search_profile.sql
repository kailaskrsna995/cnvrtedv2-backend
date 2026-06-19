-- Structured search facets extracted from ICP + UserContext at onboarding.
-- Agents build queries deterministically from these instead of one-shot LLM guesses.
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS search_profile JSONB;
