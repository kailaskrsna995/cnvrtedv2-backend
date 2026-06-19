"""
LIST ASSEMBLY
=============
Runs daily at 7am per user timezone (and on-demand when user triggers).
Assembles the final ranked list of 20 leads for each profile.

Ranking order:
  1. Intent score (highest first)
  2. Freshness (newest signal first)
  3. Has email (contact info available first)

Rules:
  - Never show a lead the user has already seen (check seen_signals)
  - Max 20 leads per day per profile
  - If no live signals today → fall back to ICP-matched companies (never empty)
  - Write to daily_lists table once assembled (prevents duplicate assembly)
"""

import logging
from datetime import date, datetime
from app.database import supabase

logger = logging.getLogger(__name__)

MAX_LEADS_PER_DAY = 20


async def assemble_list(profile_id: str) -> list[dict]:
    """
    Build today's lead list for a profile.
    Returns list of lead dicts, already ranked and deduped.
    """
    today = date.today().isoformat()

    # Check if already assembled today
    existing = supabase.table("daily_lists") \
        .select("id") \
        .eq("profile_id", profile_id) \
        .eq("list_date", today) \
        .execute()

    if existing.data:
        # Already assembled — just return today's leads
        return _fetch_todays_leads(profile_id, today)

    # Get seen signal hashes for this profile
    seen = supabase.table("seen_signals") \
        .select("signal_hash") \
        .eq("profile_id", profile_id) \
        .execute()
    seen_hashes = {row["signal_hash"] for row in (seen.data or [])}

    # Get today's scored leads not yet seen
    leads_result = supabase.table("leads") \
        .select("*") \
        .eq("profile_id", profile_id) \
        .eq("list_date", today) \
        .not_.in_("signal_hash", list(seen_hashes) or ["none"]) \
        .order("intent_score", desc=True) \
        .limit(MAX_LEADS_PER_DAY) \
        .execute()

    leads = leads_result.data or []

    # Fallback: no live signals today → ICP-based potential matches
    if not leads:
        leads = await _fallback_icp_matches(profile_id, seen_hashes)

    # Sort: score → freshness → has email
    leads = _rank_leads(leads)[:MAX_LEADS_PER_DAY]

    # Record in daily_lists
    supabase.table("daily_lists").upsert({
        "profile_id": profile_id,
        "list_date":  today,
        "lead_count": len(leads),
    }, on_conflict="profile_id,list_date").execute()

    return leads


def _rank_leads(leads: list[dict]) -> list[dict]:
    """Sort by intent_score desc, then signal_date desc, then has_email."""
    return sorted(
        leads,
        key=lambda l: (
            -(l.get("intent_score") or 0),
            -(datetime.fromisoformat(l["signal_date"]).timestamp()
              if l.get("signal_date") else 0),
            -(1 if l.get("email") else 0),
        )
    )


def _fetch_todays_leads(profile_id: str, today: str) -> list[dict]:
    result = supabase.table("leads") \
        .select("*") \
        .eq("profile_id", profile_id) \
        .eq("list_date", today) \
        .order("intent_score", desc=True) \
        .limit(MAX_LEADS_PER_DAY) \
        .execute()
    return result.data or []


async def _fallback_icp_matches(profile_id: str, seen_hashes: set) -> list[dict]:
    """
    When no live signals exist, return ICP-matched companies from the moat.
    Uses the companies table — grows over time as agents run.

    TODO: implement proper ICP-to-company matching
    For now returns empty list (will be improved in Week 5)
    """
    logger.info(f"[Assembly] No live signals for {profile_id} — using ICP fallback")
    return []
