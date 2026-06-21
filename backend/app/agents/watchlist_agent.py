"""
WATCHLIST AGENT — account-based lead discovery
==============================================
Flips the discovery model: instead of hoping in-ICP companies appear in
news searches, we build a list of companies that ARE the ICP (once),
then check each for fresh trigger events every run.

Every lead from this agent is in-ICP by construction — no vector gamble.

Two phases:
  BUILD (once per profile, or top-up):
    facets (lookalike_companies, industry_terms) → Claude generates
    candidate companies → stored in watchlist_companies
  MONITOR (every run):
    for each watchlist company: Serper news "<company> funding OR launch
    OR appoints OR expands" last 30 days → any hit = signal
"""

import json
import hashlib
import httpx
import logging
import asyncio
from datetime import datetime, timezone
from anthropic import Anthropic
from app.config import SERPER_API_KEY, ANTHROPIC_API_KEY
from app.queue import signal_queue
from app.database import supabase

logger = logging.getLogger(__name__)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

SERPER_URL = "https://google.serper.dev/news"
WATCHLIST_TARGET_SIZE = 60   # companies per profile
MONITOR_BATCH = 90           # companies checked per run (covers full ~86 list; rotates oldest-checked first)


# ---------------------------------------------------------------------------
# BUILD phase
# ---------------------------------------------------------------------------

BUILD_PROMPT = """You are building a high-quality target-account list for this seller.

Their ideal customer profile:
{icp_text}

Known in-ICP companies (clients/examples): {lookalikes}
Industry: {industries}
Geography focus: {geos}

{delivery_line}

List {n} REAL, specific companies that genuinely fit this ICP. MOST IMPORTANT: prioritise close
PEERS of the known in-ICP companies above — same kind of business, same size tier, same region —
not just famous names in the broad category. Prefer growth-stage / mid-market companies that have
real, high-volume content needs.

DO NOT INCLUDE (these are wrong and make the list look unfiltered):
- Giant household-name brands that run large IN-HOUSE creative/marketing teams or locked agency
  contracts (e.g. Procter & Gamble, Nestlé, Coca-Cola, Unilever, Nike, Disney, Netflix, Spotify,
  Epic Games, Riot Games, Duolingo, Peloton) — they will not hire/adopt this seller, and listing
  them makes the list look aspirational, not real pipeline. When unsure, prefer the smaller, more
  realistically-convertible peer.
- Trade associations, industry bodies, "X Association", councils, foundations
- Media outlets, newsletters, communities, "X Insider", "X Ventures", VC firms
- Staffing/recruiting firms (e.g. Creative Circle), generic aggregators ("Commerce", "Merch.com")
- Vague/generic names ("The Creative Agency", "Global ... Solutions"), or anything you're unsure is a real single operating company
- The known companies listed above

Return ONLY a JSON array, no markdown:
[{{"company_name": "...", "company_domain": "domain.com or null", "reason": "one short line why it fits"}}, ...]"""


# Delivery-model framing — how a target qualifies depends on HOW the seller delivers.
_DELIVERY_LINES = {
    "service_or_agency": (
        "This seller is a SERVICE/AGENCY/STUDIO (done-for-you). Target companies that need high volumes "
        "of the work this seller does and would realistically OUTSOURCE it to an external provider — "
        "companies in the seller's vertical (per the dossier segments) with the relevant execution/"
        "production need. NOT companies with large in-house teams that never outsource."
    ),
    "self_serve_product": (
        "This seller is a SELF-SERVE PRODUCT/TOOL. Target companies/teams that would realistically "
        "ADOPT a self-serve or mid-market tool to do this work themselves — growth-stage companies, "
        "teams, and operators in the seller's vertical with hands-on, in-house needs."
    ),
    "marketplace_platform": (
        "This seller is a MARKETPLACE/PLATFORM. Target companies on the side the seller monetises "
        "that would join or transact on such a platform."
    ),
}


async def build_watchlist_exa(profile_id: str, icp_text: str, sp: dict, existing_names: set) -> list[dict]:
    """LIVE watchlist via Exa — real, currently-indexed companies matching the ICP
    verticals (not Sonnet recall, so far fewer defunct/outdated companies)."""
    from app.config import EXA_API_KEY
    if not EXA_API_KEY:
        return []
    from exa_py import Exa
    from urllib.parse import urlparse
    exa = Exa(api_key=EXA_API_KEY)

    terms = (sp.get("industry_terms") or [])[:5] + (sp.get("adjacent_terms") or [])[:2]
    rows, seen_domains = [], set()
    for term in terms:
        try:
            # category=company → Exa returns real company homepages (live-indexed)
            r = exa.search(f"{term} company", type="auto", category="company", num_results=8)
        except Exception as e:
            logger.warning(f"[Watchlist] Exa term '{term}' failed: {e}")
            continue
        for x in getattr(r, "results", []):
            url = x.url or ""
            dom = urlparse(url).netloc.replace("www.", "") if url else ""
            if not dom or dom in seen_domains:
                continue
            # drop noise: directories, trade orgs, generic aggregators
            if any(b in dom for b in ("linkedin.com", "wikipedia.", "crunchbase.", "commerce.com", "merch.com")):
                continue
            name = (x.title or dom).split("|")[0].split("—")[0].strip()[:50]
            nl = name.lower()
            if not name or nl in existing_names:
                continue
            if any(b in nl for b in ("association", "brand equities", "consumer brands association", ".com", "directory")):
                continue
            seen_domains.add(dom)
            existing_names.add(name.lower())
            rows.append({
                "profile_id": profile_id, "company_name": name,
                "company_domain": dom, "reason": f"live match: {term}", "source": "exa",
            })
    logger.info(f"[Watchlist] Exa produced {len(rows)} live companies")
    return rows


# Junk domains Exa returns that are NOT a company's own site (app-store mirrors,
# wikis, aggregators, directories) — drop these from precision results.
_JUNK_DOMAINS = (
    "linkedin.", "wikipedia.", "crunchbase.", "youtube.", "play.google.", "apps.apple.",
    "tracxn.", "ventureradar.", "grokipedia.", "andro.io", "appstor.", "threads.", "twitter.",
    "x.com", "facebook.", "instagram.", "everything.explained", "wedeal.", "f4.fund",
    "g2.com", "capterra.", "glassdoor.", "owler.", "pitchbook.", "similarweb.", "medium.com",
)


async def build_precision_targets(profile_id: str, dossier: dict, max_per_query: int = 8) -> list[dict]:
    """PRECISION Target List — run the seller dossier's insider Exa queries through Exa neural
    search and collect the REAL companies they return. This is the precision-primary path
    (vs the keyword build_watchlist_exa): rich semantic queries → on-target companies, niche
    names broad keyword search misses. Upserts to watchlist_companies (source='precision_exa').
    Returns the rows added."""
    from app.config import EXA_API_KEY
    if not EXA_API_KEY:
        return []
    from exa_py import Exa
    from urllib.parse import urlparse
    exa = Exa(api_key=EXA_API_KEY)

    queries = dossier.get("exa_queries", [])
    if not queries:
        logger.warning("[Precision] dossier has no exa_queries")
        return []

    existing = supabase.table("watchlist_companies").select("company_name") \
        .eq("profile_id", profile_id).execute()
    existing_names = {r["company_name"].lower() for r in (existing.data or [])}

    rows, seen = [], set()
    for q in queries:
        try:
            r = exa.search(q, type="auto", category="company", num_results=max_per_query)
        except Exception as e:
            logger.warning(f"[Precision] Exa query failed: {e}")
            continue
        for x in getattr(r, "results", []):
            url = x.url or ""
            dom = urlparse(url).netloc.replace("www.", "") if url else ""
            if not dom or dom in seen or any(b in dom for b in _JUNK_DOMAINS):
                continue
            name = (x.title or dom).split("|")[0].split("—")[0].split("-")[0].strip()[:50]
            nl = name.lower()
            if not name or nl in existing_names or nl in seen:
                continue
            seen.add(dom); seen.add(nl)
            rows.append({
                "profile_id": profile_id, "company_name": name, "company_domain": dom,
                "reason": f"precision match: {q[:60]}", "source": "precision_exa",
            })

    if rows:
        supabase.table("watchlist_companies").upsert(
            rows, on_conflict="profile_id,company_name").execute()
    logger.info(f"[Precision] {len(rows)} precise companies from {len(queries)} insider queries")
    return rows


async def build_watchlist(profile_id: str, top_up: bool = False) -> int:
    """Build in-ICP company watchlist. Exa (live) first, Sonnet tops up. Returns count added."""
    p = supabase.table("user_profiles") \
        .select("icp_text, search_profile") \
        .eq("id", profile_id).execute()
    if not p.data:
        return 0
    icp_text = p.data[0].get("icp_text", "")
    sp = p.data[0].get("search_profile") or {}

    existing = supabase.table("watchlist_companies") \
        .select("company_name").eq("profile_id", profile_id).execute()
    existing_names = {r["company_name"].lower() for r in (existing.data or [])}

    needed = WATCHLIST_TARGET_SIZE - len(existing_names)
    if needed <= 0:
        logger.info(f"[Watchlist] Already at target size ({len(existing_names)})")
        return 0

    # MIX: Sonnet (curated) + Exa (live), then a Sonnet validation pass filters the
    # whole pool — drops mega-enterprises, trade orgs, generic/junk. Best of both.
    candidates = {}  # name_lower -> row

    # Exa live candidates
    for r in await build_watchlist_exa(profile_id, icp_text, sp, set(existing_names)):
        candidates.setdefault(r["company_name"].lower(), r)

    # Sonnet curated candidates
    delivery_model = sp.get("seller_delivery_model", "self_serve_product")
    prompt = BUILD_PROMPT.format(
        icp_text=icp_text[:1200],
        lookalikes=", ".join(sp.get("lookalike_companies", [])[:8]) or "none known",
        industries=", ".join(sp.get("industry_terms", [])[:6]),
        geos=", ".join(sp.get("geo_terms", [])) or "global",
        delivery_line=_DELIVERY_LINES.get(delivery_model, _DELIVERY_LINES["self_serve_product"]),
        n=min(needed, 50),
    )
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=2500,
            messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        for c in json.loads(raw):
            name = (c.get("company_name") or "").strip()
            if name and name.lower() not in existing_names:
                candidates.setdefault(name.lower(), {
                    "profile_id": profile_id, "company_name": name,
                    "company_domain": c.get("company_domain"),
                    "reason": c.get("reason", ""), "source": "claude"})
    except Exception as e:
        logger.warning(f"[Watchlist] Sonnet candidates failed: {e}")

    pool = list(candidates.values())
    kept = await _validate_candidates(pool, icp_text, delivery_model)
    if kept:
        supabase.table("watchlist_companies").upsert(
            kept, on_conflict="profile_id,company_name").execute()
    logger.info(f"[Watchlist] {len(pool)} candidates → {len(kept)} kept after validation")
    return len(kept)


async def _validate_candidates(pool: list[dict], icp_text: str,
                               delivery_model: str = "self_serve_product") -> list[dict]:
    """Sonnet filter over the candidate pool — drops anything that isn't a real,
    right-sized, on-ICP company that would plausibly buy. Negative-prompt driven."""
    if not pool:
        return []
    listing = "\n".join(f"{i}. {c['company_name']} ({c.get('company_domain') or '?'})"
                        for i, c in enumerate(pool))
    fit_clause = {
        "service_or_agency": "genuinely fit the ICP and would realistically OUTSOURCE content/video "
                             "production to an external studio/agency (content-heavy, no large in-house studio)",
        "self_serve_product": "genuinely fit the ICP and would plausibly ADOPT a self-serve/mid-market tool",
        "marketplace_platform": "genuinely fit the ICP and would join/transact on the seller's platform",
    }.get(delivery_model, "genuinely fit the ICP and would plausibly buy")
    prompt = f"""Filter this target-account list for a seller. KEEP only real, specific operating
companies that {fit_clause}.

ICP:
{icp_text[:1200]}

DROP (set keep=false): giant household-name brands that run large in-house creative teams or locked
agency contracts (P&G, Nestlé, Coca-Cola, Unilever, Nike, Disney, Netflix, Spotify, Epic Games, Riot
Games, Duolingo, Peloton etc.), trade associations / industry bodies, media outlets / newsletters /
communities / "X Insider" / VC firms, staffing or recruiting firms, generic aggregators
("Commerce", "Merch.com"), vague names you can't confirm are one real company. When unsure between a
giant and a realistic mid-market peer, DROP the giant.

Candidates:
{listing}

Return ONLY a JSON array, one per candidate IN ORDER: [{{"i":0,"keep":true}}, ...]"""
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=1500,
            messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        verdicts = {v["i"]: v.get("keep", True) for v in json.loads(raw) if isinstance(v, dict)}
        return [c for i, c in enumerate(pool) if verdicts.get(i, True)]
    except Exception as e:
        logger.warning(f"[Watchlist] validation pass failed, keeping pool: {e}")
        return pool


def add_discovered_company(profile_id: str, company_name: str, domain: str = None, reason: str = None):
    """Called when other agents find an in-ICP company — watchlist grows itself.
    Stores the lead's actual reasoning (why) so the Target List shows a real reason."""
    try:
        supabase.table("watchlist_companies").upsert({
            "profile_id": profile_id,
            "company_name": company_name,
            "company_domain": domain,
            "reason": (reason or "matched your ICP via a live signal")[:400],
            "source": "agent_discovery",
        }, on_conflict="profile_id,company_name").execute()
    except Exception as e:
        logger.debug(f"[Watchlist] add_discovered failed: {e}")


# ---------------------------------------------------------------------------
# MONITOR phase
# ---------------------------------------------------------------------------

async def check_company(http: httpx.AsyncClient, company: dict) -> list[dict]:
    """Serper news search for fresh triggers at one company."""
    name = company["company_name"]
    query = f'"{name}" (funding OR raises OR launches OR appoints OR expands OR partnership)'
    try:
        resp = await http.post(
            SERPER_URL,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": 5, "tbs": "qdr:m"},  # last month
        )
        if resp.status_code != 200:
            return []
        hits = []
        for a in resp.json().get("news", []):
            title = a.get("title", "")
            # crude relevance check: company name must appear in title or snippet
            if name.lower() not in (title + a.get("snippet", "")).lower():
                continue
            hits.append({
                "company": company,
                "title": title,
                "snippet": a.get("snippet", ""),
                "url": a.get("link", ""),
            })
        return hits
    except Exception as e:
        logger.debug(f"[Watchlist] check failed for {name}: {e}")
        return []


async def validate_company(http: httpx.AsyncClient, company: dict, need_terms: str = None) -> dict:
    """ALIVE check for the cold list: any recent activity (last ~12mo)?
    Drops defunct companies (e.g. shut-down studios). Need-detection that
    promotes a company to Intent Leads happens separately in run()."""
    name = company["company_name"]
    try:
        resp = await http.post(
            SERPER_URL,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": f'"{name}"', "num": 5, "tbs": "qdr:y"},  # past year, any news
        )
        proof_url = proof_summary = None
        if resp.status_code == 200:
            for a in resp.json().get("news", []):
                title = a.get("title", "")
                if name.lower() in (title + a.get("snippet", "")).lower():
                    proof_url = a.get("link", "")
                    proof_summary = title
                    break
        return {
            "id": company["id"],
            "is_active": proof_url is not None,   # true = alive (recent activity)
            "proof_url": proof_url,
            "proof_summary": proof_summary,
        }
    except Exception as e:
        logger.debug(f"[Watchlist] validate failed for {name}: {e}")
        return {"id": company["id"], "is_active": None, "proof_url": None, "proof_summary": None}


async def validate_watchlist(profile_id: str, progress_cb=None) -> dict:
    """Validate every watchlist company for recent activity; store proof or mark dormant."""
    from datetime import datetime, timezone
    rows = supabase.table("watchlist_companies") \
        .select("id, company_name").eq("profile_id", profile_id).execute().data or []
    if not rows:
        return {"checked": 0, "active": 0, "dormant": 0}

    sem = asyncio.Semaphore(8)
    now = datetime.now(timezone.utc).isoformat()
    active = dormant = 0
    async with httpx.AsyncClient(timeout=15) as http:
        async def vsem(c):
            async with sem:
                return await validate_company(http, c)
        results = await asyncio.gather(*[vsem(c) for c in rows])

    for i, r in enumerate(results):
        supabase.table("watchlist_companies").update({
            "is_active": r["is_active"],
            "proof_url": r["proof_url"],
            "proof_summary": r["proof_summary"],
            "activity_checked_at": now,
        }).eq("id", r["id"]).execute()
        if r["is_active"]:
            active += 1
        else:
            dormant += 1
        if progress_cb and (i + 1) % 10 == 0:
            progress_cb({"phase": "validating", "done": i + 1, "total": len(rows)})

    logger.info(f"[Watchlist] Validated {len(rows)}: {active} active, {dormant} dormant")
    return {"checked": len(rows), "active": active, "dormant": dormant}


async def run(profile_id: str, progress_cb=None) -> dict:
    """Monitor watchlist companies for fresh triggers. Auto-builds list if empty."""
    logger.info(f"[Watchlist] Starting run for {profile_id}")

    # Auto-build on first run
    wl = supabase.table("watchlist_companies") \
        .select("id, company_name, company_domain, reason, last_checked_at, feedback") \
        .eq("profile_id", profile_id) \
        .order("last_checked_at", desc=False, nullsfirst=True) \
        .limit(MONITOR_BATCH * 2).execute()

    # skip disliked companies; prefer liked first
    companies = [c for c in (wl.data or []) if c.get("feedback") != "disliked"]
    companies.sort(key=lambda c: c.get("feedback") != "liked")
    companies = companies[:MONITOR_BATCH]
    built = 0
    if not companies:
        if progress_cb:
            progress_cb({"phase": "building watchlist (first run)"})
        built = await build_watchlist(profile_id)
        wl = supabase.table("watchlist_companies") \
            .select("id, company_name, company_domain, reason, last_checked_at") \
            .eq("profile_id", profile_id).limit(MONITOR_BATCH).execute()
        companies = wl.data or []

    if not companies:
        return {"watchlist_size": 0, "checked": 0, "queued": 0}

    # Check companies concurrently (8 at a time)
    if progress_cb:
        progress_cb({"phase": "checking companies", "total": len(companies)})
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=15) as http:
        async def check_sem(c):
            async with sem:
                return await check_company(http, c)
        all_hits = await asyncio.gather(*[check_sem(c) for c in companies])

    # Mark as checked
    now = datetime.now(timezone.utc).isoformat()
    ids = [c["id"] for c in companies]
    supabase.table("watchlist_companies").update(
        {"last_checked_at": now}).in_("id", ids).execute()

    # Queue signals
    queued = 0
    seen = set()
    for hits in all_hits:
        for h in hits:
            url = h["url"]
            company = h["company"]
            sig_hash = hashlib.sha256(f"{company['company_name']}{url}".encode()).hexdigest()
            if sig_hash in seen:
                continue
            seen.add(sig_hash)
            await signal_queue.push({
                "signal_hash":     sig_hash,
                "signal_type":     "watchlist",
                "company_name":    company["company_name"],
                "company_domain":  company.get("company_domain"),
                "raw_text":        f"{h['title']}. {h['snippet']} (Watchlist: {company.get('reason','in-ICP company')})",
                "source_url":      url,
                "source_platform": "watchlist_monitor",
                "funding_amount":  None,
                "funding_round":   None,
                "summary":         h["title"],
                "source_query":    f"watchlist:{company['company_name']}",
            })
            queued += 1

    logger.info(f"[Watchlist] Done — {len(companies)} checked, {queued} fresh triggers queued (built {built})")
    return {"watchlist_built": built, "checked": len(companies), "queued": queued}
