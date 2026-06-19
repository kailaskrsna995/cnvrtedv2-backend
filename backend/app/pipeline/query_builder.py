"""
QUERY BUILDER
=============
Builds search queries deterministically from a profile's stored search_profile
facets (built once at onboarding by profile_agent.build_search_profile).

Replaces per-run one-shot LLM query generation: consistent coverage,
zero LLM calls at query time, combinatorial breadth.

Agents fall back to their old Haiku generation if search_profile is missing
(profiles created before this feature).
"""

import logging
from app.database import supabase

logger = logging.getLogger(__name__)

FUNDING_TEMPLATES = [
    "{term} raises funding 2026",
    "{term} raises Series A 2026",
    "{term} startup seed round 2026",
]

# High-value TRIGGER templates — a raise/acquisition tied to SCALING CONTENT/VIDEO is
# the strongest funding signal (Holywater raised $22M + bought a VFX studio to scale
# video; Eros raised $150M to build an AI-media platform). Applied to the top terms so
# we surface those, not just generic "X raised money".
FUNDING_TRIGGER_TEMPLATES = [
    "{term} raises funding to scale content 2026",
    "{term} acquires production studio 2026",
    "{term} expands into video 2026",
]

NEWS_TEMPLATES = [
    "{term} launches product 2026",
    "{term} appoints CMO 2026",
    "{term} company expansion 2026",
]


def load_search_profile(profile_id: str) -> dict | None:
    """Fetch stored facets for a profile. None if not built yet."""
    try:
        result = supabase.table("user_profiles") \
            .select("search_profile").eq("id", profile_id).execute()
        if result.data and result.data[0].get("search_profile"):
            return result.data[0]["search_profile"]
    except Exception as e:
        logger.warning(f"[QueryBuilder] load failed: {e}")
    return None


def _terms(sp: dict, max_industry: int = 5, max_adjacent: int = 3) -> list[str]:
    return (sp.get("industry_terms") or [])[:max_industry] + \
           (sp.get("adjacent_terms") or [])[:max_adjacent]


def funding_queries(sp: dict, max_queries: int = 16) -> list[str]:
    """industry/adjacent terms × funding templates + content-scaling trigger queries
    + geo variants + lookalikes."""
    queries = []
    terms = _terms(sp)
    for term in terms:
        for tpl in FUNDING_TEMPLATES:
            queries.append(tpl.format(term=term))
    # Content-scaling / acquisition TRIGGER queries on the top terms — these surface the
    # high-value "raised/acquired to scale video" signal (the strongest funding lead).
    for term in terms[:3]:
        for tpl in FUNDING_TRIGGER_TEMPLATES:
            queries.append(tpl.format(term=term))
    # Geo-flavoured variants for first couple of terms
    for geo in (sp.get("geo_terms") or [])[:2]:
        for term in terms[:2]:
            queries.append(f"{term} raises funding {geo} 2026")
    # Lookalike-driven: find funding news about competitors of known clients
    for company in (sp.get("lookalike_companies") or [])[:3]:
        queries.append(f"{company} competitor raises funding 2026")
    return list(dict.fromkeys(queries))[:max_queries]


def news_queries(sp: dict, max_queries: int = 8) -> list[str]:
    """industry/adjacent terms × trigger-event templates."""
    queries = []
    for term in _terms(sp, max_industry=4, max_adjacent=2):
        for tpl in NEWS_TEMPLATES:
            queries.append(tpl.format(term=term))
    return list(dict.fromkeys(queries))[:max_queries]


def buyer_queries(sp: dict, max_queries: int = 6) -> list[str]:
    """Buyer pain phrases are already search-ready — use directly."""
    return (sp.get("buyer_pain_phrases") or [])[:max_queries]


# ---------------------------------------------------------------------------
# Query performance memory — search improves every run
# ---------------------------------------------------------------------------

AGENT_TYPE_MAP = {"funding": "funding", "news": "news", "buyer_intent": "buyer_intent"}


def filter_by_performance(queries: list[str], profile_id: str, agent: str,
                          min_runs_before_drop: int = 3) -> list[str]:
    """
    Drop queries that ran >= min_runs_before_drop times and never produced
    a single signal. Winners and unproven queries stay.
    """
    try:
        result = supabase.table("query_performance") \
            .select("query, runs, signals_queued") \
            .eq("profile_id", profile_id).eq("agent", agent).execute()
        history = {r["query"]: r for r in (result.data or [])}
        kept = []
        for q in queries:
            h = history.get(q)
            if h and h["runs"] >= min_runs_before_drop and h["signals_queued"] == 0:
                logger.info(f"[QueryBuilder] dropping dead query ({agent}): {q}")
                continue
            kept.append(q)
        # Safety: never empty the list — if every query looks dead, keep them all
        # (a profile-wide drought shouldn't silence the agent forever)
        return kept if kept else queries
    except Exception as e:
        logger.warning(f"[QueryBuilder] performance filter failed: {e}")
        return queries


def record_query_performance(profile_id: str, executed: dict,
                             signals: list[dict], passed_results: list[dict]):
    """
    Called after a scored run. Upserts per-query stats:
      runs +1, signals_queued += signals it produced, leads_passed += leads.
    `executed` = {agent: [queries that actually ran]} — so zero-result
    queries are recorded too (otherwise dead queries never get dropped).
    """
    produced: dict = {}
    # every executed query gets a row (even if it produced nothing)
    for agent, queries in (executed or {}).items():
        for q in queries:
            produced.setdefault((agent, q), {"signals": 0, "leads": 0})

    # signals each query produced
    for s in signals:
        q = s.get("source_query") or ""
        agent = AGENT_TYPE_MAP.get(s.get("signal_type", ""), s.get("signal_type", ""))
        if q:
            key = (agent, q)
            produced.setdefault(key, {"signals": 0, "leads": 0})
            produced[key]["signals"] += 1

    # leads that passed (match back via source_query carried on results)
    for r in passed_results:
        q = r.get("source_query") or ""
        agent = AGENT_TYPE_MAP.get(r.get("signal_type", ""), r.get("signal_type", ""))
        if q and (agent, q) in produced:
            produced[(agent, q)]["leads"] += 1

    if not produced:
        return
    try:
        existing = supabase.table("query_performance") \
            .select("agent, query, runs, signals_queued, leads_passed") \
            .eq("profile_id", profile_id).execute()
        hist = {(r["agent"], r["query"]): r for r in (existing.data or [])}

        rows = []
        for (agent, query), stats in produced.items():
            prev = hist.get((agent, query), {"runs": 0, "signals_queued": 0, "leads_passed": 0})
            rows.append({
                "profile_id": profile_id,
                "agent": agent,
                "query": query,
                "runs": prev["runs"] + 1,
                "signals_queued": prev["signals_queued"] + stats["signals"],
                "leads_passed": prev["leads_passed"] + stats["leads"],
                "last_run_at": "now()",
            })
        supabase.table("query_performance").upsert(
            rows, on_conflict="profile_id,agent,query").execute()
        logger.info(f"[QueryBuilder] recorded performance for {len(rows)} queries")
    except Exception as e:
        logger.warning(f"[QueryBuilder] record failed: {e}")
