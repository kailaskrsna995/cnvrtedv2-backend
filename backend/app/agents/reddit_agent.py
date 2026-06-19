"""
REDDIT AGENT
============
Runs every 2 hours globally.
Scans buyer-intent subreddits for posts asking for agency/service recommendations.

Sources:
  r/entrepreneur, r/smallbusiness, r/startups, r/SaaS,
  r/ecommerce, r/marketing, r/agency + niche subreddits from ICP

Output:
  List of RawSignal objects pushed to signal_queue

Failsafe:
  Rate limit → wait 60s, retry
  0 results  → broaden query, retry once
  API down   → log, skip run, do not crash

TODO (intern):
  1. Implement fetch_reddit_posts() using PRAW
  2. Build keyword generator from ICP text
  3. Filter posts using _has_buying_signal() from ingestion.py
  4. Push valid signals to signal_queue
  5. Log run stats to agent_runs table
"""

import logging
from app.queue import signal_queue
from app.database import supabase
from app.config import PRAW_CLIENT_ID, PRAW_CLIENT_SECRET, PRAW_USER_AGENT

logger = logging.getLogger(__name__)

BUYER_SUBREDDITS = [
    "entrepreneur", "smallbusiness", "startups", "SaaS",
    "ecommerce", "marketing", "agency", "businessowners",
]

async def run() -> dict:
    """
    Entry point called by scheduler every 2 hours.
    Returns: { signals_found, signals_queued, errors }
    """
    logger.info("[RedditAgent] Starting run")

    # TODO: implement
    # 1. Load all active user_profiles from Supabase
    # 2. Extract unique ICP keywords across all profiles
    # 3. Search subreddits using PRAW
    # 4. For each post: hash → check queue → push if new
    # 5. Write to agent_runs table

    return {"signals_found": 0, "signals_queued": 0, "errors": []}
