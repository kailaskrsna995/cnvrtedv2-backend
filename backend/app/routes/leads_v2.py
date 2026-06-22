"""
LEADS ROUTES (V2)
=================
  POST /leads/v2/run/{profile_id}     → trigger agent run in background
  GET  /leads/v2/results/{profile_id} → get results (poll this)
  GET  /leads/v2/{profile_id}         → today's lead list (full pipeline)
  PUT  /leads/v2/{lead_id}/status     → update lead status
"""

import asyncio
import logging
import os
import json
from fastapi import APIRouter, BackgroundTasks
from app.database import supabase
from app.pipeline.assembly import assemble_list
from app.models import LeadStatusUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/leads/v2", tags=["leads_v2"])

# In-memory store for background job results
# { profile_id: { status, leads, error, total, passed } }
_job_store: dict = {}

# Disk cache so a completed run SURVIVES a backend restart (the in-memory store is
# lost on restart). On 'done' we write the result here; /results falls back to it
# when memory is empty.
_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "results_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


def _cache_path(profile_id: str) -> str:
    return os.path.join(_CACHE_DIR, f"{profile_id}.json")


def _save_results_cache(profile_id: str, data: dict):
    try:
        with open(_cache_path(profile_id), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"[leads] results cache write failed: {e}")


def _load_results_cache(profile_id: str):
    try:
        with open(_cache_path(profile_id), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_results_db(profile_id: str, data: dict):
    """Durable per-profile run cache in Supabase — survives Railway redeploys (the disk
    cache + in-memory store do NOT). No-ops gracefully if the lead_runs table is missing."""
    try:
        from datetime import datetime, timezone
        # serialize to a clean, plain-type structure (no shared/odd refs) before the upsert
        clean = json.loads(json.dumps(data, default=str))
        supabase.table("lead_runs").upsert(
            {"profile_id": profile_id, "result": clean,
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="profile_id").execute()
    except Exception as e:
        logger.warning(f"[leads] DB result save failed (run the lead_runs migration?): {e}")


def _load_results_db(profile_id: str):
    try:
        r = supabase.table("lead_runs").select("result").eq("profile_id", profile_id).execute()
        if r.data:
            return r.data[0]["result"]
    except Exception:
        pass
    return None


_LEAD_FIELDS = (
    "company_name", "company_domain", "funding_round", "funding_amount", "summary",
    "source_url", "signal_type", "source_platform", "intent_score", "match_score",
    "why", "proof", "evidence_type", "passed", "outreach", "signal_count", "source_query",
)


def _prim(v):
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def _safe_lead(l: dict) -> dict:
    d = {k: l.get(k) for k in _LEAD_FIELDS}
    dst = l.get("distinct_signal_types")
    d["distinct_signal_types"] = list(dst) if isinstance(dst, (list, tuple, set)) else []
    d["sources"] = [
        {"url": s.get("url"), "summary": s.get("summary"), "signal_type": s.get("signal_type")}
        for s in (l.get("sources") or []) if isinstance(s, dict)
    ]
    return d


def _safe_payload(data: dict) -> dict:
    """Rebuild a clean, primitive-only copy of a run result — no shared/cyclic refs, bounded
    depth — so persistence can never recurse/fail regardless of what the pipeline produced."""
    stages = ((data.get("pipeline") or {}).get("stages")) or []
    return {
        "status": data.get("status"),
        "leads": [_safe_lead(l) for l in (data.get("leads") or []) if isinstance(l, dict)],
        "all": [_safe_lead(l) for l in (data.get("all") or []) if isinstance(l, dict)],
        "total_signals": data.get("total_signals"),
        "filtered": data.get("filtered"),
        "passed": data.get("passed"),
        "pipeline": {"stages": [
            {"name": s.get("name"), "status": s.get("status"), "error": s.get("error"),
             "detail": {k: _prim(v) for k, v in (s.get("detail") or {}).items()}}
            for s in stages if isinstance(s, dict)
        ]},
        "error": data.get("error"),
    }


def _persist_results(profile_id: str, data: dict):
    safe = _safe_payload(data)               # primitive-only — can't recurse
    _save_results_cache(profile_id, safe)    # disk — fast, same-deploy
    _save_results_db(profile_id, safe)       # DB — durable across deploys


def _load_results_any(profile_id: str):
    return _job_store.get(profile_id) or _load_results_db(profile_id) or _load_results_cache(profile_id)


def remove_companies_from_cache(profile_id: str, exclude_terms: list) -> int:
    """Drop excluded companies from the cached run — lets the refine chatbot make
    leads vanish instantly without a re-scan. Matches explicit names (len>=4)."""
    data = _load_results_any(profile_id)
    if not data:
        return 0
    ex = [e.lower() for e in (exclude_terms or []) if e and len(e) >= 4]
    if not ex:
        return 0

    def keep(l):
        n = (l.get("company_name") or "").lower()
        return not (n and any(e in n or n in e for e in ex))

    before = len(data.get("leads", []))
    data["leads"] = [l for l in data.get("leads", []) if keep(l)]
    if isinstance(data.get("all"), list):
        data["all"] = [l for l in data["all"] if keep(l)]
    data["passed"] = len(data.get("leads", []))
    _job_store[profile_id] = data
    _persist_results(profile_id, data)
    return before - len(data.get("leads", []))


async def _run_agent_and_score(profile_id: str):
    """Background task — runs funding agent + scores signals against profile ICP."""
    import re
    from app.agents.funding_agent import run as run_funding
    from app.agents.buyer_intent_agent import run as run_buyer_intent
    from app.queue import signal_queue
    from app.pipeline.matching import vectorise_text
    from app.pipeline.scoring import score_signal
    from app.config import VECTOR_SIMILARITY_THRESHOLD, INTENT_SCORE_THRESHOLD

    # Pipeline trace — every stage reports in/out so the UI can show
    # exactly what each module did (and where things failed)
    trace: dict = {"stages": []}

    def _stage(name: str, status: str, detail: dict = None, error: str = None):
        trace["stages"].append({"name": name, "status": status, "detail": detail or {}, "error": error})
        # push live progress so the UI updates while running
        _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

    _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

    executed_queries: dict = {}  # agent → queries that ran (for performance memory)

    try:
        # 1. Run agents — each pushes signals into the shared queue
        logger.info(f"[leads] Running funding agent for {profile_id}")
        # live sub-progress: a "running" stage the agent updates as it works
        live_stage = {"name": "Funding agent", "status": "running", "detail": {}, "error": None}
        trace["stages"].append(live_stage)
        _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

        def funding_progress(d: dict):
            live_stage["detail"] = d
            _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

        try:
            funding_stats = await run_funding(profile_id, progress_cb=funding_progress)
            executed_queries["funding"] = funding_stats.pop("_queries", [])
            live_stage["status"] = "ok"
            live_stage["detail"] = funding_stats
        except Exception as e:
            logger.error(f"[leads] Funding agent failed (continuing): {e}")
            live_stage["status"] = "failed"
            live_stage["error"] = str(e)
        _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

        logger.info(f"[leads] Running buyer intent agent for {profile_id}")
        try:
            buyer_stats = await run_buyer_intent(profile_id)
            executed_queries["buyer_intent"] = buyer_stats.pop("_queries", [])
            _stage("Buyer intent agent", "ok", buyer_stats)
        except Exception as e:
            logger.error(f"[leads] Buyer intent agent failed (continuing): {e}")
            _stage("Buyer intent agent", "failed", error=str(e))

        logger.info(f"[leads] Running news agent for {profile_id}")
        try:
            from app.agents.news_agent import run as run_news
            news_stats = await run_news(profile_id)
            executed_queries["news"] = news_stats.pop("_queries", [])
            _stage("News agent", "ok", news_stats)
        except Exception as e:
            logger.error(f"[leads] News agent failed (continuing): {e}")
            _stage("News agent", "failed", error=str(e))

        # Precision agent (Seller Brain → on-target live leads). Dossier exa_queries →
        # Exa companies → dossier-fit rank → fresh-trigger check → signals. Additive:
        # if it yields nothing the broad agents above still produce the baseline.
        logger.info(f"[leads] Running precision agent for {profile_id}")
        prec_stage = {"name": "Precision agent", "status": "running", "detail": {}, "error": None}
        trace["stages"].append(prec_stage)
        _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

        def prec_progress(d: dict):
            prec_stage["detail"] = d
            _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

        try:
            from app.agents.precision_agent import run as run_precision
            prec_stats = await run_precision(profile_id, progress_cb=prec_progress)
            prec_stage["status"] = "ok"
            prec_stage["detail"] = prec_stats
        except Exception as e:
            logger.error(f"[leads] Precision agent failed (continuing): {e}")
            prec_stage["status"] = "failed"
            prec_stage["error"] = str(e)
        _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

        logger.info(f"[leads] Running watchlist agent for {profile_id}")
        wl_stage = {"name": "Watchlist agent", "status": "running", "detail": {}, "error": None}
        trace["stages"].append(wl_stage)
        _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

        def wl_progress(d: dict):
            wl_stage["detail"] = d
            _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

        try:
            from app.agents.watchlist_agent import run as run_watchlist
            wl_stats = await run_watchlist(profile_id, progress_cb=wl_progress)
            wl_stage["status"] = "ok"
            wl_stage["detail"] = wl_stats
        except Exception as e:
            logger.error(f"[leads] Watchlist agent failed (continuing): {e}")
            wl_stage["status"] = "failed"
            wl_stage["error"] = str(e)
        _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}

        # 2. Get profile
        profile_result = supabase.table("user_profiles") \
            .select("icp_text, user_context, search_profile") \
            .eq("id", profile_id).execute()

        if not profile_result.data:
            _job_store[profile_id] = {"status": "error", "error": "Profile not found", "leads": []}
            return

        profile = profile_result.data[0]
        icp_text = profile.get("icp_text", "")
        user_context = profile.get("user_context", "")
        # how the seller delivers (studio/agency vs self-serve tool) — drives modality matching
        delivery_model = (profile.get("search_profile") or {}).get("seller_delivery_model")
        # Seller Brain dossier — the SHARED context both live + company leads are scored against
        dossier = (profile.get("search_profile") or {}).get("dossier")

        # Load existing clients to exclude
        clients_result = supabase.table("existing_clients") \
            .select("company_name, company_domain") \
            .eq("profile_id", profile_id).execute()
        client_names = {r["company_name"].lower() for r in (clients_result.data or []) if r.get("company_name")}
        client_domains = {r["company_domain"].lower() for r in (clients_result.data or []) if r.get("company_domain")}

        # Load competitors to exclude from leads (shown on their own page instead)
        comp_result = supabase.table("competitors").select("name").eq("profile_id", profile_id).execute()
        competitor_names = {r["name"].lower() for r in (comp_result.data or []) if r.get("name")}

        # 3. Pull signals from queue
        signals = await signal_queue.pop_batch(200)
        logger.info(f"[leads] {len(signals)} signals in queue")
        by_type = {}
        for s in signals:
            t = s.get("signal_type", "?")
            by_type[t] = by_type.get(t, 0) + 1
        _stage("Signal queue", "ok", {"total": len(signals), **by_type})

        def _is_existing_client(signal: dict) -> bool:
            name = (signal.get("company_name") or "").strip().lower()
            domain = (signal.get("company_domain") or "").lower()
            if domain and domain in client_domains:
                return True
            if not name:
                return False
            # fuzzy: "Kuku" signal vs "Kuku FM" client — substring match (guarded against short names)
            for cn in client_names:
                if not cn:
                    continue
                if cn == name or (len(name) >= 4 and (name in cn or cn in name)):
                    return True
            return False

        def _is_competitor(signal: dict) -> bool:
            name = (signal.get("company_name") or "").strip().lower()
            return bool(name) and name in competitor_names

        # 4. Pre-filter: drop nulls, mega-raises, and existing clients
        def parse_amount(s: str) -> float:
            if not s:
                return 0
            nums = re.findall(r"[\d.]+", s.replace(",", ""))
            if not nums:
                return 0
            val = float(nums[0])
            if "billion" in s.lower():
                val *= 1000
            return val

        # Directory/aggregator domains — these are profile pages, not trigger events
        DIRECTORY_DOMAINS = (
            "tracxn.com", "crunchbase.com", "pitchbook.com", "zaubacorp.com",
            "linkedin.com/company", "glassdoor.", "owler.com", "cbinsights.com",
            "dnb.com", "rocketreach.co", "apollo.io", "leadiq.com", "zoominfo.com",
        )

        def _is_directory(s: dict) -> bool:
            url = (s.get("source_url") or "").lower()
            return any(d in url for d in DIRECTORY_DOMAINS)

        def _passes_prefilter(s: dict) -> bool:
            if _is_existing_client(s):
                return False
            if _is_competitor(s):
                return False  # competitor — shown on its own page, not a lead
            if _is_directory(s):
                return False  # directory page, not a real trigger event
            if s.get("signal_type") == "funding":
                # funding signals need a company name and a sane raise size
                return bool(s.get("company_name")) and parse_amount(s.get("funding_amount", "")) <= 500
            # buyer_intent etc. — company name extracted later during scoring
            return bool(s.get("raw_text"))

        filtered = [s for s in signals if _passes_prefilter(s)]
        logger.info(f"[leads] {len(filtered)} signals after pre-filter")
        _stage("Pre-filter", "ok", {
            "in": len(signals), "out": len(filtered),
            "dropped_clients_or_invalid": len(signals) - len(filtered),
        })

        # 5. Semantic match + score
        # Score signals concurrently (8 at a time) — same API cost, ~6x faster wall-clock.
        # Each signal is independent and fully error-isolated.
        _sem = asyncio.Semaphore(5)
        _competitors_found: list = []

        async def process_signal(signal: dict):
            async with _sem:
              try:
                raw_text = signal.get("raw_text", "")
                if not raw_text:
                    return None
                stype = signal.get("signal_type", "funding")

                # Vector gate — skipped for watchlist (in-ICP) AND buyer_intent
                # (already relevance-filtered; short posts embed low vs ICP) AND
                # precision_exa signals (already Exa-semantic + dossier-fit ranked;
                # their short "company + headline" text embeds low vs the long ICP).
                is_precision = signal.get("source_platform") == "precision_exa"
                match_score = None
                if stype not in ("watchlist", "buyer_intent") and not is_precision:
                    signal_vector = await vectorise_text(raw_text)
                    if not signal_vector:
                        return None
                    sim_result = supabase.rpc("match_profiles", {
                        "query_vector": signal_vector,
                        "match_threshold": VECTOR_SIMILARITY_THRESHOLD,
                        "match_count": 100,
                    }).execute()
                    matches = {m["profile_id"]: m.get("similarity") for m in (sim_result.data or [])}
                    if profile_id not in matches:
                        return None
                    match_score = matches[profile_id]
                elif stype == "watchlist":
                    match_score = 1.0

                score_result = await score_signal(
                    signal_text=raw_text, signal_type=stype,
                    user_context=user_context, icp_text=icp_text,
                    delivery_model=delivery_model, dossier=dossier,
                )
                company_name = signal.get("company_name") or score_result.get("company_name") or ""

                if score_result.get("is_lead") is False:
                    return None
                if score_result.get("is_competitor") and company_name:
                    if company_name not in _competitors_found:
                        _competitors_found.append(company_name)
                    return None

                return {
                    "company_name": company_name,
                    "company_domain": signal.get("company_domain"),
                    "funding_round": signal.get("funding_round"),
                    "funding_amount": signal.get("funding_amount"),
                    "summary": signal.get("summary"),
                    "source_url": signal.get("source_url"),
                    "signal_type": stype,
                    "source_platform": signal.get("source_platform", ""),
                    "intent_score": score_result.get("score", 0),
                    "match_score": round(match_score, 2) if match_score is not None else None,
                    "why": score_result.get("why", ""),
                    "proof": score_result.get("proof", ""),
                    "evidence_type": score_result.get("evidence_type", "trigger"),
                    "passed": score_result.get("passed", False),
                    "source_query": signal.get("source_query", ""),
                }
              except Exception as e:
                logger.warning(f"[leads] signal skipped (error): {e}")
                return None

        scored = await asyncio.gather(*[process_signal(s) for s in filtered])
        results = [r for r in scored if r]
        vector_matched = len(results)

        # upsert any competitors flagged during scoring
        for comp_name in _competitors_found:
            try:
                supabase.table("competitors").upsert(
                    {"profile_id": profile_id, "name": comp_name, "url": None},
                    on_conflict="profile_id,name").execute()
            except Exception:
                pass

        _stage("Vector match + score", "ok", {"in": len(filtered), "scored": len(results)})

        # COMPANY-LEVEL DEDUP + stacking: collapse N signals per company into
        # one lead. Keep best score, merge all sources, count distinct signal types.
        # 2+ signal types = real stacking → +0.10 boost (capped).
        grouped: dict = {}
        for r in results:
            key = (r.get("company_name") or "").strip().lower()
            if not key:
                # no company name (some buyer_intent) — keep as its own row
                key = f"_anon_{r.get('source_url','')}"
            g = grouped.get(key)
            if not g:
                grouped[key] = {**r, "sources": [], "signal_types": set()}
                g = grouped[key]
            # track every source article
            if r.get("source_url"):
                g["sources"].append({
                    "url": r["source_url"],
                    "summary": r.get("summary", ""),
                    "signal_type": r.get("signal_type"),
                })
            g["signal_types"].add(r.get("signal_type"))
            # keep the highest-scoring signal's score/why as the lead's headline
            # keep the best match_score seen across this company's signals
            if r.get("match_score") is not None and (g.get("match_score") is None or r["match_score"] > g["match_score"]):
                g["match_score"] = r["match_score"]
            if r["intent_score"] > g["intent_score"]:
                g["intent_score"] = r["intent_score"]
                g["why"] = r["why"]
                g["proof"] = r.get("proof", "")
                g["evidence_type"] = r.get("evidence_type", "trigger")
                g["summary"] = r.get("summary", g.get("summary", ""))
                g["source_url"] = r.get("source_url", g.get("source_url", ""))
                g["signal_type"] = r.get("signal_type", g.get("signal_type"))

        deduped = []
        for g in grouped.values():
            types = g.pop("signal_types")
            g["signal_count"] = len(g["sources"])
            g["distinct_signal_types"] = sorted(types)
            # stacking boost: corroborated across 2+ signal types
            if len(types) >= 2:
                g["intent_score"] = min(0.99, g["intent_score"] + 0.10)
                g["why"] = f"[{len(types)} signal types: {', '.join(sorted(types))}] " + g["why"]
            g["passed"] = g["intent_score"] >= INTENT_SCORE_THRESHOLD
            deduped.append(g)

        boosted = sum(1 for g in deduped if len(g["distinct_signal_types"]) >= 2)
        _stage("Scoring + dedup", "ok", {
            "raw_signals": len(results),
            "unique_companies": len(deduped),
            "multi_signal": boosted,
        })

        results = deduped
        results.sort(key=lambda x: x["intent_score"], reverse=True)
        passed = [r for r in results if r["passed"]]
        _stage(f"Threshold (≥{INTENT_SCORE_THRESHOLD})", "ok", {"unique_companies": len(results), "passed": len(passed)})

        # FINAL STRICT GATE — profile-aware Sonnet judge (universal: reasons against
        # THIS seller's ICP/offering). Cuts competitors, wrong-vertical, weak triggers
        # that Haiku rationalized through. Flagged competitors → competitors table.
        try:
            from app.pipeline.scoring import judge_leads
            verdict = await judge_leads(passed, user_context, icp_text, delivery_model, dossier)
            kept = verdict["keep"]
            for comp_name in verdict["competitors"]:
                try:
                    supabase.table("competitors").upsert(
                        {"profile_id": profile_id, "name": comp_name, "url": None},
                        on_conflict="profile_id,name").execute()
                except Exception:
                    pass
            _stage("Final judge (Sonnet)", "ok", {
                "in": len(passed), "kept": len(kept),
                "cut": len(passed) - len(kept), "competitors_flagged": len(verdict["competitors"]),
            })
            passed = kept
        except Exception as e:
            logger.error(f"[leads] judge failed (keeping all): {e}")

        # OUTREACH — one batched Sonnet call writes a personalized opener per final
        # lead. Fully error-isolated (leads returned unchanged on failure).
        if passed:
            try:
                from app.pipeline.scoring import generate_outreach
                passed = await generate_outreach(passed, user_context, icp_text, dossier)
                _stage("Outreach lines (Sonnet)", "ok", {
                    "leads": len(passed),
                    "written": sum(1 for l in passed if l.get("outreach")),
                })
            except Exception as e:
                logger.error(f"[leads] outreach failed (continuing): {e}")
                _stage("Outreach lines (Sonnet)", "failed", error=str(e))

        # LinkedIn contact enrichment — DISABLED in the run (Apify over free-tier budget
        # + slow timeouts hang the whole scan). Contacts are available on-demand via the
        # cold-list "Find contacts" button instead. Re-enable when Apify is funded.
        from app.config import APIFY_API_TOKEN
        ENABLE_LEAD_CONTACT_ENRICH = False
        if ENABLE_LEAD_CONTACT_ENRICH and APIFY_API_TOKEN and passed:
            ln_stage = {"name": "LinkedIn contact finder", "status": "running", "detail": {}, "error": None}
            trace["stages"].append(ln_stage)
            _job_store[profile_id] = {"status": "running", "leads": [], "error": None, "pipeline": trace}
            try:
                from app.agents.linkedin_agent import enrich_leads_with_contacts
                enriched = await enrich_leads_with_contacts(passed, max_lookups=8)
                ln_stage["status"] = "ok"
                ln_stage["detail"] = {"leads_checked": min(8, len(passed)), "contacts_found": enriched}
            except Exception as e:
                logger.error(f"[leads] LinkedIn enrichment failed (continuing): {e}")
                ln_stage["status"] = "failed"
                ln_stage["error"] = str(e)

        # Self-growing watchlist: ONLY real company-typed leads (funding/news) with a
        # clean company name get added. Never buyer_intent (garbage names/handles) or
        # watchlist (already there). Keeps the cold list to actual companies.
        def _clean_company(name: str) -> bool:
            n = (name or "").strip()
            if len(n) < 2 or len(n) > 50:
                return False
            bad = ["blog", "handle not provided", "reddit", "news", "guide", "how ", "best ", " tips"]
            return not any(b in n.lower() for b in bad)

        try:
            from app.agents.watchlist_agent import add_discovered_company
            for r in passed:
                if (r.get("signal_type") in ("funding", "news")
                        and _clean_company(r.get("company_name"))):
                    add_discovered_company(profile_id, r["company_name"], r.get("company_domain"), reason=r.get("why"))
        except Exception as e:
            logger.debug(f"[leads] watchlist feedback failed: {e}")

        # Record per-query outcomes — dead queries get dropped on future runs
        try:
            from app.pipeline.query_builder import record_query_performance
            record_query_performance(profile_id, executed_queries, signals, passed)
            _stage("Query memory", "ok", {
                "queries_tracked": sum(len(v) for v in executed_queries.values()),
            })
        except Exception as e:
            _stage("Query memory", "failed", error=str(e))

        logger.info(f"[leads] Done — {len(passed)} leads passed for {profile_id}")
        final = {
            "status": "done",
            "leads": passed,
            "all": results,
            "total_signals": len(signals),
            "filtered": len(filtered),
            "passed": len(passed),
            "pipeline": trace,
            "error": None,
        }
        _job_store[profile_id] = final
        try:
            _persist_results(profile_id, final)  # disk + DB (survives Railway redeploys)
        except Exception as e:
            # persistence must NEVER discard a good run — leads stay in memory either way
            logger.error(f"[leads] persist failed (leads kept in memory): {e}")

    except Exception as e:
        logger.error(f"[leads] Background job failed: {e}")
        _job_store[profile_id] = {"status": "error", "error": str(e), "leads": [], "pipeline": trace}


@router.post("/run/{profile_id}")
async def trigger_run(profile_id: str, background_tasks: BackgroundTasks):
    """Trigger agent + scoring in background. Returns immediately."""
    if _job_store.get(profile_id, {}).get("status") == "running":
        return {"status": "already_running"}
    _job_store[profile_id] = {"status": "running", "leads": []}
    background_tasks.add_task(_run_agent_and_score, profile_id)
    return {"status": "started"}


@router.get("/results/{profile_id}")
async def get_results(profile_id: str):
    """Poll this to get results after triggering a run.
    Falls back to the disk cache (last completed run) when memory is empty —
    so a backend restart doesn't blank the dashboard."""
    job = _job_store.get(profile_id)
    if job:
        return job
    cached = _load_results_db(profile_id) or _load_results_cache(profile_id)
    if cached:
        return {**cached, "from_cache": True}
    return {"status": "idle"}


@router.get("/{profile_id}")
async def get_leads(profile_id: str):
    leads = await assemble_list(profile_id)
    live = [l for l in leads if l.get("signal_type") != "icp_match"]
    warm = [l for l in leads if l.get("signal_type") == "icp_match"]
    return {"profile_id": profile_id, "live_signals": live, "potential_matches": warm, "total": len(leads)}


@router.post("/{profile_id}/refresh")
async def refresh_leads(profile_id: str):
    leads = await assemble_list(profile_id)
    return {"total": len(leads), "leads": leads}


@router.get("/coldlist/{profile_id}")
async def get_cold_list(profile_id: str, include_dormant: bool = False):
    """The target-company cold list (watchlist companies + contacts + proof).
    Hides disliked + dormant (no recent activity); liked + proven float to top."""
    _sel = ("company_name, company_domain, reason, contact_name, contact_title, "
            "contact_linkedin, contact_email, contact_phone, feedback, proof_url, proof_summary, is_active")
    try:
        r = supabase.table("watchlist_companies").select(_sel).eq("profile_id", profile_id).execute()
    except Exception:
        # contact_phone column not added to the DB yet — degrade gracefully
        r = supabase.table("watchlist_companies") \
            .select(_sel.replace(", contact_phone", "")).eq("profile_id", profile_id).execute()
    rows = [x for x in (r.data or []) if x.get("feedback") != "disliked"]
    # dormant = explicitly checked and found inactive (e.g. defunct studio)
    if not include_dormant:
        rows = [x for x in rows if x.get("is_active") is not False]
    # Read-time enrichment: swap the generic "discovered by agent" placeholder for the
    # actual lead reasoning (the why) pulled from the cached run — so existing rows show
    # a real reason without a re-scan.
    cached = _load_results_any(profile_id)
    if cached:
        why_by = {}
        for l in (cached.get("leads") or []) + (cached.get("all") or []):
            n = (l.get("company_name") or "").lower()
            if n and l.get("why") and n not in why_by:
                why_by[n] = l["why"]
        for x in rows:
            rsn = x.get("reason") or ""
            if "discovered by agent" in rsn or not rsn:
                w = why_by.get((x.get("company_name") or "").lower())
                if w:
                    x["reason"] = w
    # liked first, then proven (has proof), then has-contact, then rest
    rows.sort(key=lambda x: (x.get("feedback") != "liked", not x.get("proof_url"), not x.get("contact_name")))
    with_contact = sum(1 for x in rows if x.get("contact_name"))
    with_proof = sum(1 for x in rows if x.get("proof_url"))
    return {"total": len(rows), "with_contact": with_contact, "with_proof": with_proof, "companies": rows}


_validate_jobs: dict = {}


async def _run_validate(profile_id: str):
    from app.agents.watchlist_agent import validate_watchlist
    _validate_jobs[profile_id] = {"status": "running"}
    try:
        res = await validate_watchlist(profile_id)
        _validate_jobs[profile_id] = {"status": "done", **res}
    except Exception as e:
        _validate_jobs[profile_id] = {"status": "error", "error": str(e)}


@router.post("/coldlist/{profile_id}/validate")
async def validate_cold_list(profile_id: str, background_tasks: BackgroundTasks):
    """Check every watchlist company for recent activity → proof or drop-if-dormant."""
    if _validate_jobs.get(profile_id, {}).get("status") == "running":
        return {"status": "already_running"}
    background_tasks.add_task(_run_validate, profile_id)
    return {"status": "started"}


@router.get("/coldlist/{profile_id}/validate-status")
async def validate_status(profile_id: str):
    return _validate_jobs.get(profile_id, {"status": "idle"})


@router.post("/coldlist/{profile_id}/feedback")
async def coldlist_feedback(profile_id: str, body: dict):
    """Record like/dislike on a watchlist company (grows-with-user loop)."""
    name = body.get("company_name")
    fb = body.get("feedback")  # 'liked' | 'disliked' | null (clear)
    if not name:
        return {"error": "company_name required"}
    supabase.table("watchlist_companies").update({"feedback": fb}) \
        .eq("profile_id", profile_id).eq("company_name", name).execute()
    return {"status": "ok", "company_name": name, "feedback": fb}


# in-memory enrichment progress {profile_id: {status, done, total}}
_coldlist_jobs: dict = {}


async def _enrich_cold_list(profile_id: str, limit: int):
    """Find contacts via Apollo (name + title + email + LinkedIn) for watchlist companies
    that have a domain. On-demand only — triggered by the 'Find contacts' button."""
    from app.agents.apollo_agent import find_contact
    from datetime import datetime, timezone
    # buyer titles from the dossier (fallback to a sensible generic set)
    prof = supabase.table("user_profiles").select("search_profile").eq("id", profile_id).execute()
    dossier = ((prof.data[0].get("search_profile") if prof.data else None) or {}).get("dossier") or {}
    titles = dossier.get("buyer_titles") or [
        "Head of Marketing", "VP Marketing", "CMO", "Founder", "CEO",
        "Head of Content", "Director of Marketing", "Head of Growth",
    ]
    rows = supabase.table("watchlist_companies") \
        .select("id, company_name, company_domain, contact_name") \
        .eq("profile_id", profile_id).execute().data or []
    # Apollo needs a domain; only companies without a contact yet
    todo = [r for r in rows if not r.get("contact_name") and r.get("company_domain")][:limit]
    _coldlist_jobs[profile_id] = {"status": "running", "done": 0, "total": len(todo)}
    for i, row in enumerate(todo):
        try:
            c = await find_contact(row["company_name"], row.get("company_domain"), titles)
            if c and (c.get("name") or c.get("email")):
                payload = {
                    "contact_name": c.get("name"),
                    "contact_title": c.get("title"),
                    "contact_email": c.get("email"),
                    "contact_phone": c.get("phone"),
                    "contact_linkedin": c.get("linkedin"),
                    "contact_checked_at": datetime.now(timezone.utc).isoformat(),
                }
                try:
                    supabase.table("watchlist_companies").update(payload).eq("id", row["id"]).execute()
                except Exception:
                    # contact_phone column not added yet — store everything else
                    payload.pop("contact_phone", None)
                    supabase.table("watchlist_companies").update(payload).eq("id", row["id"]).execute()
        except Exception as e:
            logger.warning(f"[coldlist] apollo enrich failed for {row['company_name']}: {e}")
        _coldlist_jobs[profile_id] = {"status": "running", "done": i + 1, "total": len(todo)}
    _coldlist_jobs[profile_id] = {"status": "done", "done": len(todo), "total": len(todo)}


@router.post("/coldlist/{profile_id}/enrich")
async def enrich_cold_list(profile_id: str, background_tasks: BackgroundTasks, limit: int = 20):
    """Find decision-maker contacts for watchlist companies (background, cached)."""
    if _coldlist_jobs.get(profile_id, {}).get("status") == "running":
        return {"status": "already_running", **_coldlist_jobs[profile_id]}
    background_tasks.add_task(_enrich_cold_list, profile_id, limit)
    return {"status": "started"}


@router.get("/coldlist/{profile_id}/enrich-status")
async def cold_list_enrich_status(profile_id: str):
    return _coldlist_jobs.get(profile_id, {"status": "idle"})


@router.post("/{profile_id}/remove-lead")
async def remove_lead(profile_id: str, body: dict):
    """Manually remove a single lead from the cached run (the X button on a row)."""
    name = (body.get("company_name") or "").strip().lower()
    if not name:
        return {"removed": 0}
    data = _load_results_any(profile_id)
    if not data:
        return {"removed": 0}
    before = len(data.get("leads", []))
    data["leads"] = [l for l in data.get("leads", []) if (l.get("company_name") or "").strip().lower() != name]
    if isinstance(data.get("all"), list):
        data["all"] = [l for l in data["all"] if (l.get("company_name") or "").strip().lower() != name]
    data["passed"] = len(data.get("leads", []))
    _job_store[profile_id] = data
    _persist_results(profile_id, data)
    return {"removed": before - len(data.get("leads", []))}


@router.post("/{profile_id}/export-notion")
async def export_notion(profile_id: str, body: dict):
    """Export the current list into the founder's Notion as a new database. Body =
    { title, columns:[{name,type}], rows:[{colName:value}] }. Returns {url, written}."""
    from app.agents.notion_export import export_to_notion
    return await export_to_notion(
        body.get("title", "cnvrted export"),
        body.get("columns", []),
        body.get("rows", []),
    )


@router.get("/competitors/{profile_id}")
async def list_competitors(profile_id: str):
    r = supabase.table("competitors").select("name, url").eq("profile_id", profile_id).execute()
    return {"competitors": r.data or []}


@router.get("/clients/{profile_id}")
async def list_clients(profile_id: str):
    result = supabase.table("existing_clients") \
        .select("id, company_name, company_domain") \
        .eq("profile_id", profile_id).execute()
    return {"clients": result.data or []}


@router.post("/clients/{profile_id}")
async def add_client(profile_id: str, body: dict):
    name = (body.get("company_name") or "").strip() or None
    domain = (body.get("company_domain") or "").strip().lower() or None
    if not name and not domain:
        return {"error": "company_name or company_domain required"}
    row = {"profile_id": profile_id, "company_name": name, "company_domain": domain}
    supabase.table("existing_clients").upsert(row, on_conflict="profile_id,company_name").execute()
    return {"status": "added"}


@router.delete("/clients/{client_id}")
async def remove_client(client_id: str):
    supabase.table("existing_clients").delete().eq("id", client_id).execute()
    return {"status": "deleted"}


@router.put("/{lead_id}/status")
async def update_lead_status(lead_id: str, body: LeadStatusUpdate):
    supabase.table("leads").update({"status": body.status}).eq("id", lead_id).execute()
    if body.status == "dismissed":
        lead = supabase.table("leads").select("profile_id, signal_id").eq("id", lead_id).execute()
        if lead.data:
            row = lead.data[0]
            signal = supabase.table("signals").select("signal_hash").eq("id", row["signal_id"]).execute()
            if signal.data:
                supabase.table("seen_signals").upsert({
                    "profile_id": row["profile_id"],
                    "signal_hash": signal.data[0]["signal_hash"],
                    "action": "dismissed",
                }, on_conflict="profile_id,signal_hash").execute()
    return {"status": "updated"}
