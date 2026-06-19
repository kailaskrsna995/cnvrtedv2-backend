"""
SCHEDULER
=========
Runs the 5 agents on their fixed intervals using APScheduler.
Starts automatically when the FastAPI server starts.

Schedule:
  Reddit Agent       every 2 hours
  Buyer Intent Agent every 3 hours
  Funding Agent      every 6 hours
  Hiring Agent       every 6 hours
  News Agent         every 12 hours

Each agent run:
  1. Finds signals
  2. Pushes to queue
  3. Queue worker processes: match → score → enrich → outreach → save

TODO: replace with proper job queue (Celery/ARQ) when scaling beyond 1 server
"""

import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.agents import reddit_agent, funding_agent, hiring_agent, buyer_intent_agent, news_agent
from app.pipeline.matching import find_matching_profiles
from app.pipeline.scoring import score_signal
from app.pipeline.enrichment import enrich_company
from app.pipeline.outreach import generate_outreach_line
from app.queue import signal_queue
from app.database import supabase

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


def start_scheduler():
    """Call this from main.py on startup."""

    scheduler.add_job(
        reddit_agent.run,
        IntervalTrigger(hours=2),
        id="reddit_agent",
        name="Reddit Agent",
        replace_existing=True,
    )
    scheduler.add_job(
        buyer_intent_agent.run,
        IntervalTrigger(hours=3),
        id="buyer_intent_agent",
        name="Buyer Intent Agent",
        replace_existing=True,
    )
    scheduler.add_job(
        funding_agent.run,
        IntervalTrigger(hours=6),
        id="funding_agent",
        name="Funding Agent",
        replace_existing=True,
    )
    scheduler.add_job(
        hiring_agent.run,
        IntervalTrigger(hours=6),
        id="hiring_agent",
        name="Hiring Agent",
        replace_existing=True,
    )
    scheduler.add_job(
        news_agent.run,
        IntervalTrigger(hours=12),
        id="news_agent",
        name="News Agent",
        replace_existing=True,
    )
    scheduler.add_job(
        process_queue,
        IntervalTrigger(seconds=30),
        id="queue_worker",
        name="Queue Worker",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("[Scheduler] Started — all agents scheduled")


def stop_scheduler():
    scheduler.shutdown()


async def process_queue():
    """
    Queue worker — runs every 30 seconds.
    Pops signals from queue and runs them through the pipeline:
    Match → Score → Enrich → Outreach → Save as Lead
    """
    batch = await signal_queue.pop_batch(n=20)
    if not batch:
        return

    logger.info(f"[Queue] Processing {len(batch)} signals")

    for signal in batch:
        try:
            await _process_signal(signal)
        except Exception as e:
            logger.error(f"[Queue] Failed to process signal: {e}")


async def _process_signal(signal: dict):
    """Full pipeline for one signal."""
    raw_text    = signal.get("raw_text", "")
    signal_type = signal.get("signal_type", "")
    signal_hash = signal.get("signal_hash", "")

    # 1. Save signal to DB
    supabase.table("signals").upsert(signal, on_conflict="signal_hash").execute()

    # 2. Match to profiles
    matched_profiles = await find_matching_profiles(raw_text)
    if not matched_profiles:
        return

    # 3. For each matched profile: score + enrich + outreach + save lead
    for match in matched_profiles:
        profile_id = match["profile_id"]

        # Load profile context
        profile_result = supabase.table("user_profiles") \
            .select("user_context, icp_text") \
            .eq("id", profile_id).execute()
        if not profile_result.data:
            continue

        profile   = profile_result.data[0]
        icp_text  = profile.get("icp_text", "")
        user_ctx  = profile.get("user_context", "")

        # Score
        score_result = await score_signal(raw_text, signal_type, user_ctx, icp_text)
        if not score_result.get("passed"):
            continue

        # Enrich
        domain   = signal.get("company_domain", "")
        enriched = await enrich_company(domain, signal.get("company_name", ""))

        # Outreach
        outreach = await generate_outreach_line(
            user_context=user_ctx,
            signal_type=signal_type,
            why_flagged=score_result.get("why", ""),
            decision_maker_name=enriched.get("decision_maker", ""),
            decision_maker_title=enriched.get("title", ""),
        )

        # Save lead
        supabase.table("leads").insert({
            "profile_id":      profile_id,
            "signal_id":       None,  # TODO: get signal DB id
            "company_name":    signal.get("company_name"),
            "company_url":     signal.get("company_url"),
            "company_domain":  domain,
            "signal_type":     signal_type,
            "why_flagged":     score_result.get("why"),
            "intent_score":    score_result.get("score"),
            "decision_maker":  enriched.get("decision_maker"),
            "title":           enriched.get("title"),
            "email":           enriched.get("email"),
            "phone":           enriched.get("phone"),
            "linkedin_url":    enriched.get("linkedin_url"),
            "outreach_line":   outreach,
            "source_url":      signal.get("source_url"),
            "signal_date":     signal.get("signal_date"),
        }).execute()

        logger.info(f"[Pipeline] Lead saved — {signal.get('company_name')} → profile {profile_id}")
