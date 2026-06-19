"""
FUNDING AGENT
=============
Finds companies that just raised money — budget is now unlocked.

Optimised pipeline:
  1. Serper News search (global + ICP-specific queries)
  2. Regex pre-filter — drop articles without funding keywords (free)
  3. Trafilatura fetch full article text (free, falls back to snippet)
  4. Embed full text → vector match vs ICP (cheap, accurate)
  5. Batch Haiku extraction — 5 articles per call (only on matches)
  6. Haiku scoring with prompt caching
"""

import re
import asyncio
import hashlib
import httpx
import json
import logging
from datetime import datetime, timezone, timedelta
from anthropic import Anthropic
from app.config import SERPER_API_KEY, ANTHROPIC_API_KEY
from app.queue import signal_queue
from app.database import supabase

logger = logging.getLogger(__name__)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

SERPER_URL = "https://google.serper.dev/news"

# Regex: article must contain funding signal to pass pre-filter
FUNDING_REGEX = re.compile(
    r'\b(raises?|raised|funding|funded|seed round|series [a-e]|pre-seed|'
    r'investment|investor|venture|capital|backed|closes? round)\b',
    re.IGNORECASE
)
AMOUNT_REGEX = re.compile(r'[\$€£]\s*[\d,.]+\s*[MBKmbk]?(?:illion|illion)?', re.IGNORECASE)
ROUND_REGEX  = re.compile(r'\b(seed|pre-seed|series [a-e]|series [A-E]|bridge|growth)\b', re.IGNORECASE)

# Try importing trafilatura — falls back to snippet if not installed
try:
    import trafilatura
    HAS_TRAFILATURA = True
    logger.info("[FundingAgent] trafilatura available")
except ImportError:
    HAS_TRAFILATURA = False
    logger.info("[FundingAgent] trafilatura not installed — using snippets")

# Vertical-agnostic safety net — a SMALL set for breadth. Kept lean on purpose: the old
# 12-query list (Series A/B/C/seed/pre-seed + B2B/consumer) flooded the pool with off-vertical
# noise (OpenAI/Anthropic/defense/chips/fintech) that the profile FACET queries don't, then had
# to be filtered out downstream — wasting vector+scoring budget. The profile's own
# industry/adjacent facet queries now do the targeted work; these just catch the long tail.
GLOBAL_QUERIES = [
    "startup raises funding 2026",
    "company closes funding round 2026",
    "startup raises funding India 2026",
]

# Free RSS feeds — different geographies/stages than Serper's top-10
RSS_FEEDS = [
    "https://techcrunch.com/category/venture/feed/",
    "https://www.finsmes.com/feed",
    "https://www.eu-startups.com/feed/",
    "https://inc42.com/feed/",
]


# ---------------------------------------------------------------------------
# Recency filter
# ---------------------------------------------------------------------------

def _is_recent(article: dict, max_days: int = 90) -> bool:
    raw = (article.get("date") or "").strip().lower()
    if not raw:
        return False
    try:
        if any(w in raw for w in ("hour", "minute", "just now")):
            return True
        if "day" in raw:
            return int(raw.split()[0]) <= max_days
        if "week" in raw:
            return int(raw.split()[0]) * 7 <= max_days
        if "month" in raw:
            return int(raw.split()[0]) <= max_days // 30
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
        parsed = datetime.strptime(raw, "%b %d, %Y").replace(tzinfo=timezone.utc)
        return parsed >= cutoff
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Serper search
# ---------------------------------------------------------------------------

async def search_serper_news(query: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                SERPER_URL,
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": 10, "gl": "us", "hl": "en", "tbs": "qdr:m3"},
            )
            if resp.status_code == 200:
                articles = resp.json().get("news", [])
                recent = [a for a in articles if _is_recent(a)]
                logger.info(f"[FundingAgent] '{query}' → {len(articles)} articles, {len(recent)} recent")
                return recent
            logger.warning(f"[FundingAgent] Serper {resp.status_code} for: {query}")
            return []
    except Exception as e:
        logger.error(f"[FundingAgent] Serper failed: {e}")
        return []


# ---------------------------------------------------------------------------
# RSS feeds (free, no API)
# ---------------------------------------------------------------------------

def _parse_rss_date(raw: str) -> bool:
    """True if RSS pubDate is within 90 days."""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(raw)
        return dt >= datetime.now(timezone.utc) - timedelta(days=90)
    except Exception:
        return True  # keep if unparseable — feeds are recent by nature


async def fetch_rss_feeds() -> list[dict]:
    """Pull articles from free funding-news RSS feeds. Returns Serper-shaped dicts."""
    import xml.etree.ElementTree as ET
    articles = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
        for feed_url in RSS_FEEDS:
            try:
                resp = await http.get(feed_url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    continue
                root = ET.fromstring(resp.content)
                for item in root.iter("item"):
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    desc = re.sub(r"<[^>]+>", "", item.findtext("description") or "")[:500]
                    pub = item.findtext("pubDate") or ""
                    if title and link and _parse_rss_date(pub):
                        articles.append({
                            "title": title,
                            "snippet": desc.strip(),
                            "link": link,
                            "date": "1 day ago",  # RSS items are recent; recency checked above
                        })
            except Exception as e:
                logger.warning(f"[FundingAgent] RSS failed for {feed_url}: {e}")
    logger.info(f"[FundingAgent] RSS: {len(articles)} articles from {len(RSS_FEEDS)} feeds")
    return articles


# ---------------------------------------------------------------------------
# Layer 1: Regex pre-filter (free)
# ---------------------------------------------------------------------------

def regex_prefilter(articles: list[dict]) -> list[dict]:
    passed = []
    for a in articles:
        text = f"{a.get('title', '')} {a.get('snippet', '')}"
        if FUNDING_REGEX.search(text):
            passed.append(a)
    dropped = len(articles) - len(passed)
    if dropped:
        logger.info(f"[FundingAgent] Regex dropped {dropped}/{len(articles)} articles")
    return passed


# ---------------------------------------------------------------------------
# Layer 2: Trafilatura full-text fetch (free)
# ---------------------------------------------------------------------------

async def fetch_full_text(article: dict) -> str:
    """Get full article text via trafilatura, fall back to title+snippet."""
    title   = article.get("title", "")
    snippet = article.get("snippet", "")
    url     = article.get("link", "")
    base    = f"{title}. {snippet}"

    if not HAS_TRAFILATURA or not url:
        return base

    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                full = trafilatura.extract(resp.text, include_comments=False, include_tables=False)
                if full and len(full) > 300:
                    return full[:4000]
    except Exception as e:
        logger.debug(f"[FundingAgent] trafilatura fetch failed for {url}: {e}")

    return base


# ---------------------------------------------------------------------------
# Layer 3: Embed + vector match (cheap, accurate)
# ---------------------------------------------------------------------------

async def vector_filter(articles_with_text: list[dict], profile_id: str, threshold: float,
                        progress_cb=None) -> list[dict]:
    """
    Embed full article text and check vector similarity against the ICP.
    Embeddings run 8 at a time (same total API calls, just concurrent).
    """
    from app.pipeline.matching import vectorise_text

    sem = asyncio.Semaphore(8)
    done = 0
    total = len(articles_with_text)

    async def check(item: dict):
        nonlocal done
        async with sem:
            text = item.get("full_text", "")
            vec = await vectorise_text(text) if text else None
            done += 1
            if progress_cb and done % 10 == 0:
                progress_cb({"phase": "vector matching", "done": done, "total": total})
            if not vec:
                return None
            result = supabase.rpc("match_profiles", {
                "query_vector": vec,
                "match_threshold": threshold,
                "match_count": 100,
            }).execute()
            matched_ids = [m["profile_id"] for m in (result.data or [])]
            if profile_id in matched_ids:
                item["signal_vector"] = vec
                return item
            return None

    results = await asyncio.gather(*[check(i) for i in articles_with_text])
    matched = [r for r in results if r]
    logger.info(f"[FundingAgent] Vector filter: {len(matched)}/{total} matched ICP")
    return matched


# ---------------------------------------------------------------------------
# Layer 4: Batch Haiku extraction (5 articles per call)
# ---------------------------------------------------------------------------

BATCH_EXTRACT_PROMPT = """Extract funding information from these news articles.

For each article return a JSON object. Return a JSON array of objects, one per article, in order.
If an article is not clearly a funding announcement set is_funding_news to false.

Articles:
{articles}

Return ONLY a valid JSON array, no markdown:
[
  {{
    "is_funding_news": true,
    "company_name": "exact name or null",
    "company_domain": "domain.com or null",
    "funding_amount": "$5M or null",
    "funding_round": "seed/Series A/Series B/other or null",
    "summary": "one sentence"
  }},
  ...
]"""


def _try_regex_extract(text: str) -> dict:
    """Try to extract amount and round via regex before calling Claude."""
    amount_match = AMOUNT_REGEX.search(text)
    round_match  = ROUND_REGEX.search(text)
    return {
        "funding_amount": amount_match.group(0).strip() if amount_match else None,
        "funding_round":  round_match.group(0).strip()  if round_match  else None,
    }


async def batch_extract(items: list[dict]) -> list[dict]:
    """Extract structured data from up to 5 articles in one Haiku call."""
    results = []
    batch_size = 5

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]

        # Try regex first — skip Claude if we can parse it
        articles_needing_claude = []
        regex_results = {}

        for idx, item in enumerate(batch):
            text = item.get("full_text", "")
            regex = _try_regex_extract(text)
            title = item.get("article", {}).get("title", "")
            # If regex got both amount and round, skip Claude for this one
            if regex["funding_amount"] and regex["funding_round"]:
                regex_results[idx] = {
                    "is_funding_news": True,
                    "company_name": None,  # Claude still needed for name — included in next pass
                    "company_domain": None,
                    **regex,
                    "summary": title,
                    "_needs_name": True,
                }
            else:
                articles_needing_claude.append((idx, item))

        # Call Claude only for articles regex couldn't fully parse
        if articles_needing_claude:
            articles_text = "\n\n".join([
                f"Article {j+1}:\nTitle: {item.get('article', {}).get('title', '')}\n"
                f"Text: {item.get('full_text', '')[:600]}"
                for j, (_, item) in enumerate(articles_needing_claude)
            ])
            try:
                resp = claude.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=600,
                    messages=[{"role": "user", "content": BATCH_EXTRACT_PROMPT.format(articles=articles_text)}]
                )
                raw = resp.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1].lstrip("json").strip()
                parsed = json.loads(raw)
                for j, (orig_idx, item) in enumerate(articles_needing_claude):
                    if j < len(parsed):
                        regex_results[orig_idx] = parsed[j]
            except Exception as e:
                logger.warning(f"[FundingAgent] Batch extract failed: {e}")

        # Merge results back in order
        for idx, item in enumerate(batch):
            extracted = regex_results.get(idx, {})
            if not extracted.get("is_funding_news"):
                continue
            results.append({
                "article":  item.get("article", {}),
                "full_text": item.get("full_text", ""),
                "extracted": extracted,
                "signal_vector": item.get("signal_vector"),
            })

    logger.info(f"[FundingAgent] Batch extract: {len(results)} funding signals from {len(items)} articles")
    return results


# ---------------------------------------------------------------------------
# ICP query generation
# ---------------------------------------------------------------------------

async def generate_icp_queries(icp_text: str) -> list[str]:
    prompt = f"""Generate exactly 3 Google News search queries to find funding announcements for companies matching this ICP.

ICP:
{icp_text[:1500]}

Rules:
- Include "raises funding" or "seed round" or "Series A" or "Series B"
- Include industry keywords from the ICP
- Under 10 words each
- End with 2026

Return ONLY a JSON array:
["query one 2026", "query two 2026", "query three 2026"]"""

    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        queries = json.loads(raw)
        return queries if isinstance(queries, list) else []
    except Exception as e:
        logger.warning(f"[FundingAgent] ICP query gen failed: {e}")
        return []


async def get_all_icp_queries() -> list[str]:
    if not supabase:
        return []
    try:
        result = supabase.table("user_profiles") \
            .select("id, icp_text") \
            .eq("is_active", True) \
            .not_.is_("icp_text", "null") \
            .execute()
        all_queries = []
        for profile in (result.data or []):
            if profile.get("icp_text"):
                queries = await generate_icp_queries(profile["icp_text"])
                all_queries.extend(queries)
        return list(set(all_queries))
    except Exception as e:
        logger.error(f"[FundingAgent] Failed to fetch ICP queries: {e}")
        return []


# ---------------------------------------------------------------------------
# Signal dedup
# ---------------------------------------------------------------------------

def make_signal_hash(company_name: str, source_url: str) -> str:
    return hashlib.sha256(f"{company_name or ''}{source_url or ''}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Main run (called from leads_v2.py per profile, or scheduler globally)
# ---------------------------------------------------------------------------

async def run(profile_id: str = None, threshold: float = None, progress_cb=None) -> dict:
    from app.config import VECTOR_SIMILARITY_THRESHOLD
    threshold = threshold or VECTOR_SIMILARITY_THRESHOLD

    def progress(d: dict):
        if progress_cb:
            try:
                progress_cb(d)
            except Exception:
                pass

    logger.info(f"[FundingAgent] Starting run (profile_id={profile_id})")
    seen_hashes: set = set()

    # 1. Build query list — search_profile facets first, Haiku fallback
    from app.pipeline.query_builder import load_search_profile, funding_queries, filter_by_performance
    queries = list(GLOBAL_QUERIES)
    if profile_id:
        sp = load_search_profile(profile_id)
        if sp:
            facet_queries = funding_queries(sp)
            queries.extend(facet_queries)
            logger.info(f"[FundingAgent] {len(facet_queries)} facet queries: {facet_queries}")
            queries = filter_by_performance(queries, profile_id, "funding")
        else:
            try:
                p = supabase.table("user_profiles").select("icp_text").eq("id", profile_id).execute()
                if p.data and p.data[0].get("icp_text"):
                    icp_queries = await generate_icp_queries(p.data[0]["icp_text"])
                    queries.extend(icp_queries)
                    logger.info(f"[FundingAgent] {len(icp_queries)} Haiku fallback queries")
            except Exception as e:
                logger.warning(f"[FundingAgent] Could not load ICP queries: {e}")
    else:
        queries.extend(await get_all_icp_queries())

    # 2. Fetch articles — all Serper queries + RSS concurrently
    progress({"phase": "searching", "queries": len(queries)})

    async def search_tagged(q: str) -> list[dict]:
        articles = await search_serper_news(q)
        for a in articles:
            a["_query"] = q
        return articles

    search_results = await asyncio.gather(
        *[search_tagged(q) for q in queries], fetch_rss_feeds()
    )
    all_articles = []
    for batch in search_results[:-1]:
        all_articles.extend(batch)
    for a in search_results[-1]:
        a["_query"] = "rss_feeds"
    all_articles.extend(search_results[-1])

    logger.info(f"[FundingAgent] {len(all_articles)} total articles before dedup")

    # Deduplicate by URL
    seen_urls = set()
    unique_articles = []
    for a in all_articles:
        url = a.get("link", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_articles.append(a)

    logger.info(f"[FundingAgent] {len(unique_articles)} after URL dedup")

    # 3. Regex pre-filter
    filtered = regex_prefilter(unique_articles)

    # 4. Fetch full text via trafilatura — 10 concurrent
    progress({"phase": "fetching full text", "articles": len(filtered)})
    fetch_sem = asyncio.Semaphore(5)
    fetched = 0

    async def fetch_one(article: dict) -> dict:
        nonlocal fetched
        async with fetch_sem:
            full_text = await fetch_full_text(article)
            fetched += 1
            if fetched % 15 == 0:
                progress({"phase": "fetching full text", "done": fetched, "total": len(filtered)})
            return {"article": article, "full_text": full_text}

    items_with_text = list(await asyncio.gather(*[fetch_one(a) for a in filtered]))

    # 5. Vector match — only proceed with ICP-relevant articles
    if profile_id:
        items_with_text = await vector_filter(items_with_text, profile_id, threshold, progress_cb=progress_cb)
    # If global run (no profile_id), skip vector filter — leads_v2 does per-profile matching

    # 6. Batch Haiku extraction
    progress({"phase": "extracting (Haiku)", "matched": len(items_with_text)})
    extracted_signals = await batch_extract(items_with_text)

    # 7. Push to queue
    queued = 0
    for item in extracted_signals:
        ext     = item["extracted"]
        article = item["article"]
        company = ext.get("company_name") or ""
        url     = article.get("link", "")

        sig_hash = make_signal_hash(company, url)
        if sig_hash in seen_hashes:
            continue
        seen_hashes.add(sig_hash)

        signal = {
            "signal_hash":     sig_hash,
            "signal_type":     "funding",
            "company_name":    company,
            "company_domain":  ext.get("company_domain"),
            "raw_text":        item["full_text"][:2000],
            "source_url":      url,
            "source_platform": "serper_news",
            "funding_amount":  ext.get("funding_amount"),
            "funding_round":   ext.get("funding_round"),
            "summary":         ext.get("summary", article.get("title", "")),
            "signal_vector":   item.get("signal_vector"),
            "source_query":    article.get("_query", ""),
        }
        await signal_queue.push(signal)
        queued += 1
        logger.info(f"[FundingAgent] Queued: {company} — {ext.get('funding_round')} {ext.get('funding_amount')}")

    logger.info(f"[FundingAgent] Done — {len(unique_articles)} articles → {len(filtered)} passed regex → {len(extracted_signals)} extracted → {queued} queued")
    return {
        "total_articles": len(unique_articles),
        "passed_regex":   len(filtered),
        "matched_vector": len(items_with_text),
        "extracted":      len(extracted_signals),
        "queued":         queued,
        "_queries":       queries,  # for performance tracking (hidden in UI)
    }
