"""
AGENTS ROUTES
=============
  POST /agents/trigger         → manual Run Now (once per hour per profile)
  GET  /agents/status          → last run time + signals found per agent
  GET  /agents/queue           → how many signals are currently in queue
"""

from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timedelta
from app.models import AgentTriggerRequest
from app.database import supabase
from app.queue import signal_queue
from app.agents import reddit_agent, funding_agent, hiring_agent, buyer_intent_agent, news_agent
from app.auth import get_current_user, assert_owner

router = APIRouter(prefix="/agents", tags=["agents"])

# In-memory rate limit tracker: profile_id → last trigger time
# TODO: move to Redis when Redis is added (Day 11)
_last_trigger: dict[str, datetime] = {}
TRIGGER_COOLDOWN_MINUTES = 60


@router.post("/trigger")
async def trigger_agents(body: AgentTriggerRequest, user: dict = Depends(get_current_user)):
    """
    Manual Run Now. Enforces 1 trigger per profile per hour.
    Runs all 5 agents (or subset if agent_names specified).
    """
    profile_id = body.profile_id
    assert_owner(profile_id, user)
    now = datetime.utcnow()

    # Rate limit check
    last = _last_trigger.get(profile_id)
    if last and (now - last) < timedelta(minutes=TRIGGER_COOLDOWN_MINUTES):
        wait_mins = TRIGGER_COOLDOWN_MINUTES - int((now - last).total_seconds() / 60)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Next run available in {wait_mins} minutes."
        )

    _last_trigger[profile_id] = now

    # Determine which agents to run
    agents_to_run = body.agent_names or ["reddit", "funding", "hiring", "buyer_intent", "news"]

    results = {}
    for agent_name in agents_to_run:
        try:
            agent_map = {
                "reddit":       reddit_agent.run,
                "funding":      funding_agent.run,
                "hiring":       hiring_agent.run,
                "buyer_intent": buyer_intent_agent.run,
                "news":         news_agent.run,
            }
            if agent_name in agent_map:
                result = await agent_map[agent_name]()
                results[agent_name] = result
        except Exception as e:
            results[agent_name] = {"error": str(e)}

    return {
        "triggered_at": now.isoformat(),
        "profile_id": profile_id,
        "results": results,
        "queue_size": signal_queue.size(),
    }


@router.get("/status")
async def get_agent_status(user: dict = Depends(get_current_user)):
    """Return last run stats for each agent."""
    result = supabase.table("agent_runs") \
        .select("agent_name, status, signals_found, started_at, completed_at, error_message") \
        .order("started_at", desc=True) \
        .limit(10) \
        .execute()
    return result.data or []


@router.get("/queue")
async def get_queue_size(user: dict = Depends(get_current_user)):
    """How many signals are waiting to be processed."""
    return {"queue_size": signal_queue.size()}
