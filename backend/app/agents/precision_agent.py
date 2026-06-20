"""
PRECISION AGENT — Seller Brain → on-target live leads
=====================================================
The broad funding/news agents cast a WIDE net and lean on the vector gate to throw
most of it away (the 122→25 waste). This agent is the PRECISION-primary path:

  1. Run the dossier's insider exa_queries through Exa neural search (company category)
     → on-target companies that broad keyword search misses (Holywater/ReelShort-class).
  2. rank_by_dossier_fit — one batched Sonnet pass scores every company against the
     dossier's ranked segments + need-signals; keep only the high-fit ones. (This is the
     precision↔recall gate: high Exa recall → clean, ranked precision.)
  3. For each high-fit company, check for a FRESH trigger event (Serper recent news).
     A company is only a LIVE lead if there's a reason-to-act NOW; otherwise it's a
     Target List entry (handled by build_precision_targets), not a lead.
  4. Companies with a fresh trigger → push into the shared signal queue as `news`
     signals so they flow through the existing scoring/judge/outreach pipeline unchanged.

ADDITIVE BY DESIGN: this only ADDS signals. If Exa/ranking/Serper fail, the existing
funding/news/buyer/watchlist agents still produce the working baseline.
"""

import hashlib
import asyncio
import logging
from urllib.parse import urlparse

from app.queue import signal_queue
from app.database import supabase
from app.agents.watchlist_agent import _JUNK_DOMAINS

logger = logging.getLogger(__name__)

# How many top-fit companies to keep before the (cost-bearing) Serper trigger check.
MAX_RANKED = 18
# Exa results per insider query.
MAX_PER_QUERY = 8
# Dossier-fit floor — below this a company isn't worth a trigger lookup.
FIT_THRESHOLD = 0.6
# Exa search mode. 'deep' = agentic, higher recall — surfaces niche/dream-class names
# ('auto' missed Holywater Tech; 'deep' pulled it naturally). ~3-4x slower per query
# (~7s vs ~2s) and costs more credits; worth it for the on-target recall. Falls back to
# 'auto' automatically if a deep call errors.
EXA_SEARCH_TYPE = "deep"


def _make_hash(company: str, url: str) -> str:
    return hashlib.sha256(f"{company or ''}{url or ''}".encode()).hexdigest()


async def _exa_companies(dossier: dict) -> list[dict]:
    """Run the dossier's insider exa_queries → de-duped candidate companies."""
    from app.config import EXA_API_KEY
    if not EXA_API_KEY:
        logger.info("[Precision] no EXA_API_KEY — skipping precision sourcing")
        return []
    queries = dossier.get("exa_queries", [])
    if not queries:
        logger.warning("[Precision] dossier has no exa_queries")
        return []

    from exa_py import Exa
    exa = Exa(api_key=EXA_API_KEY)

    async def _exa_search(q: str):
        """Deep agentic search; fall back to auto if a deep call errors."""
        try:
            return await asyncio.to_thread(
                exa.search, q, type=EXA_SEARCH_TYPE, category="company", num_results=MAX_PER_QUERY
            )
        except Exception as e:
            logger.warning(f"[Precision] Exa '{EXA_SEARCH_TYPE}' failed ({e}); retrying auto")
            return await asyncio.to_thread(
                exa.search, q, type="auto", category="company", num_results=MAX_PER_QUERY
            )

    candidates, seen = [], set()
    for q in queries:
        try:
            r = await _exa_search(q)
        except Exception as e:
            logger.warning(f"[Precision] Exa query failed: {e}")
            continue
        for x in getattr(r, "results", []):
            url = getattr(x, "url", "") or ""
            dom = urlparse(url).netloc.replace("www.", "") if url else ""
            if not dom or dom in seen or any(b in dom for b in _JUNK_DOMAINS):
                continue
            title = getattr(x, "title", None) or dom
            name = title.split("|")[0].split("—")[0].split("-")[0].strip()[:50]
            nl = name.lower()
            if not name or nl in seen:
                continue
            seen.add(dom)
            seen.add(nl)
            candidates.append({
                "company_name": name,
                "company_domain": dom,
                "summary": title,
                "source_url": url,
                "_query": q,
            })
    logger.info(f"[Precision] {len(candidates)} candidate companies from {len(queries)} insider queries")
    return candidates


async def _find_trigger(company: str) -> dict | None:
    """One Serper recent-news lookup for a company. Returns the first article that reads
    as a TRIGGER event (launch/hire/expansion/raise/milestone), else None."""
    from app.agents.news_agent import search_serper_news, TRIGGER_REGEX
    try:
        articles = await search_serper_news(company)
    except Exception as e:
        logger.debug(f"[Precision] trigger search failed for {company}: {e}")
        return None
    for a in articles:
        text = f"{a.get('title','')} {a.get('snippet','')}"
        # must mention the company AND read as a trigger event
        if company.lower() not in text.lower():
            continue
        if TRIGGER_REGEX.search(text):
            return a
    return None


async def run(profile_id: str, progress_cb=None) -> dict:
    """Precision live-lead sourcing. Returns stats; pushes `news` signals into the queue."""
    logger.info(f"[Precision] Starting run (profile_id={profile_id})")

    def progress(d: dict):
        if progress_cb:
            try:
                progress_cb(d)
            except Exception:
                pass

    # Dossier is the whole point — no dossier, nothing to do (broad agents cover it).
    p = supabase.table("user_profiles").select("search_profile").eq("id", profile_id).execute()
    dossier = ((p.data[0].get("search_profile") if p.data else None) or {}).get("dossier")
    if not dossier:
        logger.info("[Precision] no dossier — skipping (broad agents cover this profile)")
        return {"skipped": "no_dossier", "queued": 0}

    # 1. Exa neural search → candidate companies
    progress({"phase": "exa_search"})
    candidates = await _exa_companies(dossier)
    if not candidates:
        return {"candidates": 0, "ranked": 0, "with_trigger": 0, "queued": 0}

    # 2. Dossier-fit gate — wide recall → ranked precision
    progress({"phase": "ranking", "candidates": len(candidates)})
    try:
        from app.pipeline.scoring import rank_by_dossier_fit
        ranked = await rank_by_dossier_fit(candidates, dossier, keep_threshold=FIT_THRESHOLD)
    except Exception as e:
        logger.warning(f"[Precision] dossier-fit ranking failed, using candidates raw: {e}")
        ranked = candidates
    ranked = ranked[:MAX_RANKED]
    logger.info(f"[Precision] {len(ranked)} companies kept after dossier-fit gate")

    # 3. Fresh-trigger check (concurrency-limited) → only act-now companies become leads
    progress({"phase": "trigger_check", "ranked": len(ranked)})
    _sem = asyncio.Semaphore(5)

    async def _check(c: dict):
        async with _sem:
            return c, await _find_trigger(c["company_name"])

    checked = await asyncio.gather(*[_check(c) for c in ranked])

    # 4. Queue the triggered ones as `news` signals (flow through existing pipeline)
    queued = 0
    seen_hashes: set = set()
    for c, article in checked:
        if not article:
            continue
        url = article.get("link", "") or c.get("source_url", "")
        h = _make_hash(c["company_name"], url)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        seg = c.get("segment", "")
        fit = c.get("fit")
        raw_text = (
            f"{c['company_name']} — precision-matched ICP company"
            + (f" (segment: {seg})" if seg else "")
            + f". {article.get('title','')}. {article.get('snippet','')}"
        )
        await signal_queue.push({
            "signal_hash":     h,
            "signal_type":     "news",
            "company_name":    c["company_name"],
            "company_domain":  c.get("company_domain"),
            "raw_text":        raw_text,
            "source_url":      url,
            "source_platform": "precision_exa",
            "funding_amount":  None,
            "funding_round":   "precision",
            "summary":         article.get("title", c.get("summary", "")),
            "source_query":    c.get("_query", ""),
        })
        queued += 1

    with_trigger = sum(1 for _, a in checked if a)
    logger.info(f"[Precision] Done — {queued} precision leads queued "
                f"({with_trigger}/{len(ranked)} had a fresh trigger)")
    return {
        "candidates": len(candidates),
        "ranked": len(ranked),
        "with_trigger": with_trigger,
        "queued": queued,
        "fit_threshold": FIT_THRESHOLD,
    }
