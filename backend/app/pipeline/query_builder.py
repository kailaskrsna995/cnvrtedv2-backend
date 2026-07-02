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

import json
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


def load_dossier(profile_id: str) -> dict | None:
    """The Seller Brain dossier (stored under search_profile.dossier). None if not built."""
    sp = load_search_profile(profile_id)
    return (sp or {}).get("dossier")


# ---------------------------------------------------------------------------
# Dossier-driven Serper queries (Seller Brain → precise trigger search)
# ---------------------------------------------------------------------------
# The funding/news agents historically prepended a generic GLOBAL_QUERIES list
# ("startup raises funding 2026") that floods the pool with off-vertical noise
# (OpenAI/defense/chips/fintech) which the vector gate then has to throw away —
# wasting embedding + scoring budget (the 122→25 vector-gate waste). When a
# Seller Brain dossier exists we have something far better: ranked core_segments
# + observable need_signals. One small Haiku call turns those into tight, on-ICP
# Serper queries. The caller drops GLOBAL_QUERIES when these are available.

_DOSSIER_QUERY_PROMPT = """You write Google News search queries to find {kind_desc} at companies
that match a seller's TARGET DOSSIER. The queries must surface ON-TARGET companies only — no
generic startup/tech noise.

SELLER OFFERING: {offering}

RANKED TARGET SEGMENTS (highest-fit first — weight queries toward the top ones):
{segments}

OBSERVABLE NEED SIGNALS (the events that mean a company needs this seller):
{need_signals}

GEO: {geo}

Write {n} Google News queries that would surface companies in the TOP segments hitting one of the
{kind_word} need-signals. Rules:
- Each query under 9 words, end with 2026.
- Use the concrete product-category words from the segments above (the seller's actual vertical),
  NOT abstract jargon.
- {kind_rule}
- No company names. No "startup" alone. No off-vertical filler.

Return ONLY a JSON array of {n} strings."""

_KIND_CONFIG = {
    "funding": {
        "kind_desc": "FUNDING events (raises, rounds, acquisitions to scale content)",
        "kind_word": "funding",
        "kind_rule": 'Each query must include a funding word: "raises funding" / "Series A" / '
                     '"Series B" / "seed round" / "acquires".',
    },
    "news": {
        "kind_desc": "TRIGGER events (launches, exec hires, expansion, content slate)",
        "kind_word": "trigger-event",
        "kind_rule": 'Each query must include a trigger word: "launches" / "appoints" '
                     '/ "expands into" / "new product" / "partners with".',
    },
}


async def dossier_queries(dossier: dict, kind: str, max_queries: int = 8) -> list[str]:
    """Generate tight Serper queries grounded in the dossier's ranked core_segments
    + need_signals. kind = 'funding' | 'news'. Returns [] on any failure so the caller
    can fall back to facet/global queries."""
    if not dossier or kind not in _KIND_CONFIG:
        return []
    try:
        from app.llm import Anthropic
        from app.config import ANTHROPIC_API_KEY
        cfg = _KIND_CONFIG[kind]
        segs = sorted(dossier.get("core_segments", []), key=lambda s: s.get("fit", 0), reverse=True)
        seg_lines = "\n".join(f"  - [{s.get('fit')}] {s.get('name','')}" for s in segs[:5]) or "  (none)"
        need_lines = "\n".join(f"  - {s}" for s in (dossier.get("need_signals") or [])[:8]) or "  (none)"
        prompt = _DOSSIER_QUERY_PROMPT.format(
            kind_desc=cfg["kind_desc"], kind_word=cfg["kind_word"], kind_rule=cfg["kind_rule"],
            offering=(dossier.get("offering") or "")[:300],
            segments=seg_lines, need_signals=need_lines,
            geo=(dossier.get("geo") or "global")[:160], n=max_queries,
        )
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        out = json.loads(raw)
        queries = [q for q in out if isinstance(q, str) and q.strip()][:max_queries]
        logger.info(f"[QueryBuilder] {len(queries)} dossier {kind} queries: {queries}")
        return queries
    except Exception as e:
        logger.warning(f"[QueryBuilder] dossier {kind} query gen failed: {e}")
        return []


def buyer_queries(sp: dict, max_queries: int = 6) -> list[str]:
    """Prefer the dossier's buyer_language (richer, insider buyer-voice); fall back to
    the search_profile buyer_pain_phrases for profiles without a dossier."""
    d = sp.get("dossier") or {}
    phrases = d.get("buyer_language") or sp.get("buyer_pain_phrases") or []
    return phrases[:max_queries]


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
