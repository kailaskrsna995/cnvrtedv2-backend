"""
HIRING AGENT
============
Runs every 6 hours globally.
Finds companies posting marketing/growth roles — they need outside help NOW.

Sources:
  - Serper API → searches Greenhouse, Lever, Workable, Ashby
  - Jina Reader → scrapes job posting for company details

Signal logic:
  "Head of Marketing" job post → company has marketing gap → signal_type = "hiring"

Roles to watch:
  Head of Marketing, VP Marketing, Growth Lead, CMO, Marketing Manager,
  Performance Marketing, Paid Social Manager, Content Lead, Brand Manager

Failsafe:
  Serper limit → cache results, use cached
  Auth wall    → skip + log

TODO (intern):
  1. Implement search_job_boards() using Serper
  2. For each result: use Jina Reader to extract company details
  3. Extract role title + company name + domain
  4. Build RawSignal and push to queue
  5. Log to agent_runs table
"""

import logging
from app.queue import signal_queue
from app.database import supabase
from app.config import SERPER_API_KEY

logger = logging.getLogger(__name__)

MARKETING_ROLES = [
    "Head of Marketing",
    "VP Marketing",
    "CMO",
    "Growth Lead",
    "Performance Marketing Manager",
    "Paid Social Manager",
    "Content Marketing Lead",
    "Brand Manager",
]

JOB_BOARDS = [
    "site:greenhouse.io",
    "site:lever.co",
    "site:jobs.ashbyhq.com",
    "site:apply.workable.com",
]

async def run() -> dict:
    """Entry point called by scheduler every 6 hours."""
    logger.info("[HiringAgent] Starting run")

    # TODO: implement
    # 1. For each role in MARKETING_ROLES + each board in JOB_BOARDS
    # 2. Search via Serper: "Head of Marketing site:greenhouse.io"
    # 3. Scrape job page with Jina Reader for company info
    # 4. Build signal + push to queue

    return {"signals_found": 0, "signals_queued": 0, "errors": []}
