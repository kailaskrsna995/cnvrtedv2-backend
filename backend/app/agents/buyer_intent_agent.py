"""
BUYER INTENT AGENT — in-house semantic search ("mini-Exa")
==========================================================
Finds companies PUBLICLY SAYING they need help — the hottest leads possible.
Funding agent infers intent; this agent finds stated intent.

Architecture (4 layers, we own all of them):
  1. DISCOVERY — Reddit (PRAW), HN (free API), LinkedIn posts (via Serper)
  2. FETCHING  — APIs directly; Scrapling/httpx for blocked URLs
  3. EMBEDDING — OpenAI (matching.vectorise_text)
  4. RANKING   — pgvector match vs ICP → Haiku score (same pipeline as funding)

Each source degrades gracefully:
  Reddit  → skipped if PRAW creds missing
  HN      → always works (free public API)
  LinkedIn→ works with SERPER_API_KEY (Google has indexed the posts;
            we read title+snippet from Serper, fetch full post if possible)
"""

import re
import json
import hashlib
import logging
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from app.llm import Anthropic
from app import usage
from app.config import (
    SERPER_API_KEY, ANTHROPIC_API_KEY,
    PRAW_CLIENT_ID, PRAW_CLIENT_SECRET, PRAW_USER_AGENT,
    EXA_API_KEY, APIFY_API_TOKEN, APIFY_LINKEDIN_ACTOR,
)
from app.queue import signal_queue
from app.database import supabase

logger = logging.getLogger(__name__)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

# Buyer language — a post must look like someone seeking help/services
BUYER_REGEX = re.compile(
    r'\b(looking for|recommend|recommendation|suggestions? for|need help|'
    r'need an?|hiring an?|searching for|anyone know|who do you use|'
    r'outsourc|struggling with|need to find|best agency|good agency|'
    r'freelancer or agency|vendor|service provider)\b',
    re.IGNORECASE
)

# Subreddits where agency buyers post (broad set; vector match narrows per-ICP)
SUBREDDITS = [
    "startups", "Entrepreneur", "smallbusiness", "marketing",
    "digital_marketing", "SaaS", "ecommerce", "podcasting",
    "NewTubers", "videography", "advertising", "PPC", "content_marketing",
]

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
SERPER_SEARCH_URL = "https://google.serper.dev/search"

# Reddit (optional)
try:
    import praw
    HAS_PRAW = bool(PRAW_CLIENT_ID and PRAW_CLIENT_SECRET)
except ImportError:
    HAS_PRAW = False


# ---------------------------------------------------------------------------
# Query generation — ICP → buyer-language queries
# ---------------------------------------------------------------------------

async def build_buyer_queries(icp_text: str) -> list[str]:
    """
    Generate search phrases in the BUYER's natural language.
    Buyers don't say 'seeking agency partner' — they say 'our edits are killing us'.
    """
    prompt = f"""An agency targets this ideal customer:
{icp_text[:1200]}

Generate 5 SHORT keyword search queries (2-5 words each) to find posts where
buyers are seeking this agency's type of service. These run on keyword search
engines (Google, HN search) — so use common, simple words buyers actually use,
not long sentences.

Good examples: "recommend video editing agency", "podcast clips editor",
"looking for marketing agency", "outsource short form video"

Return ONLY a JSON array of 5 short strings."""
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        queries = json.loads(raw)
        return queries if isinstance(queries, list) else []
    except Exception as e:
        logger.warning(f"[BuyerIntent] Query gen failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Source 1: Reddit (PRAW) — activates when creds are set
# ---------------------------------------------------------------------------

def search_reddit(queries: list[str], max_age_days: int = 30) -> list[dict]:
    if not HAS_PRAW:
        logger.info("[BuyerIntent] Reddit skipped (no PRAW creds)")
        return []
    posts = []
    try:
        reddit = praw.Reddit(
            client_id=PRAW_CLIENT_ID,
            client_secret=PRAW_CLIENT_SECRET,
            user_agent=PRAW_USER_AGENT,
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).timestamp()
        sub = reddit.subreddit("+".join(SUBREDDITS))
        for query in queries[:3]:  # limit API usage
            for post in sub.search(query, sort="new", time_filter="month", limit=10):
                if post.created_utc < cutoff:
                    continue
                posts.append({
                    "title": post.title,
                    "text": (post.selftext or "")[:2000],
                    "url": f"https://reddit.com{post.permalink}",
                    "author": str(post.author),
                    "platform": "reddit",
                    "posted_at": datetime.fromtimestamp(post.created_utc, timezone.utc).isoformat(),
                    "query": query,
                })
        logger.info(f"[BuyerIntent] Reddit: {len(posts)} posts")
    except Exception as e:
        logger.error(f"[BuyerIntent] Reddit failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Source 2: Hacker News (Algolia API — free, no auth)
# ---------------------------------------------------------------------------

async def search_hn(queries: list[str], max_age_days: int = 30) -> list[dict]:
    posts = []
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=max_age_days)).timestamp())
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            for query in queries[:3]:
                resp = await http.get(HN_SEARCH_URL, params={
                    "query": query,
                    "tags": "(story,ask_hn)",
                    "numericFilters": f"created_at_i>{cutoff_ts}",
                    "hitsPerPage": 10,
                })
                if resp.status_code != 200:
                    continue
                for hit in resp.json().get("hits", []):
                    posts.append({
                        "title": hit.get("title") or "",
                        "text": (hit.get("story_text") or "")[:2000],
                        "url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                        "author": hit.get("author", ""),
                        "platform": "hackernews",
                        "posted_at": hit.get("created_at", ""),
                        "query": query,
                    })
        logger.info(f"[BuyerIntent] HN: {len(posts)} posts")
    except Exception as e:
        logger.error(f"[BuyerIntent] HN failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Source 3: LinkedIn posts via Serper (Google indexed them already)
# ---------------------------------------------------------------------------

async def search_linkedin_posts(queries: list[str]) -> list[dict]:
    """LinkedIn post discovery via Google — no LinkedIn login.
    NOTE: free Serper rejects `site:` operators, so we append "linkedin" as a keyword
    and filter organic results to linkedin.com links (same fix as the Reddit path)."""
    if not SERPER_API_KEY:
        return []
    posts = []
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            async def one_query(query: str) -> list[dict]:
                out = []
                resp = await http.post(
                    SERPER_SEARCH_URL,
                    headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                    json={"q": f"{query} linkedin", "num": 20, "tbs": "qdr:m"},
                )
                usage.log_serper()
                if resp.status_code != 200:
                    return out
                for r in resp.json().get("organic", []):
                    link = r.get("link", "")
                    if "linkedin.com" not in link:
                        continue  # keep only LinkedIn results
                    out.append({
                        "title": r.get("title", ""),
                        "text": r.get("snippet", "")[:2000],
                        "url": link,
                        "author": "",
                        "platform": "linkedin",
                        "posted_at": "",
                        "query": query,
                    })
                return out

            batches = await asyncio.gather(*[one_query(q) for q in queries[:6]])
            for b in batches:
                posts.extend(b)
        logger.info(f"[BuyerIntent] LinkedIn: {len(posts)} posts")
    except Exception as e:
        logger.error(f"[BuyerIntent] LinkedIn search failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Source: Exa — whole-web semantic search (finds posts keyword search misses)
# ---------------------------------------------------------------------------

def _exa_client():
    from app.exa_client import Exa
    return Exa(api_key=EXA_API_KEY)


def build_exa_rich_query(icp_text: str, service_desc: str = "") -> str:
    """Turn the full ICP into ONE rich, context-loaded Exa query. Exa is neural —
    a paragraph describing the in-market buyer beats keyword phrases. One Haiku call."""
    prompt = f"""Write ONE search query (2-4 sentences, natural language) for a neural
search engine to find RECENT public posts/discussions where a real buyer is ACTIVELY
looking for help with the service below — asking for recommendations, hiring, or seeking
a studio/tool. Describe the ideal buyer and the situation they'd be in. Do not return
keywords or a list — return a flowing description a buyer's post would semantically match.

Service the seller offers: {service_desc[:300]}
Ideal customer profile:
{icp_text[:1200]}

Return ONLY the query text, nothing else."""
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text.strip().strip('"')
    except Exception as e:
        logger.warning(f"[BuyerIntent] rich query gen failed: {e}")
        return ""


async def search_exa(queries: list[str], rich_query: str = "") -> list[dict]:
    """Semantic web search via Exa. Primary: ONE rich context-loaded query (uses the
    full ICP). Secondary: a couple buyer-voice phrases for breadth."""
    if not EXA_API_KEY:
        return []
    # Restrict to user-generated-content domains where BUYERS post — not vendor
    # marketing pages. Without this, a "buyer looking for X" query returns
    # competitor landing pages (they describe the buyer's pain to sell to them).
    # NOTE: reddit/twitter/x are blocked on Exa free tier (premium sources).
    # Reddit is covered separately via Serper. Exa covers these allowed UGC sources.
    UGC_DOMAINS = ["news.ycombinator.com", "quora.com", "indiehackers.com",
                   "linkedin.com", "threads.net", "medium.com", "dev.to"]
    posts = []
    # (label_for_tracking, actual_query_sent, num_results)
    exa_queries = []
    if rich_query:
        exa_queries.append(("rich_icp_query", rich_query, 15))
    for p in queries[:2]:
        exa_queries.append((p, f"{p} — a real post where someone is asking for this", 8))
    try:
        exa = _exa_client()
        for pain, query, n in exa_queries:
            try:
                r = exa.search(query, type="auto", num_results=n,
                               include_domains=UGC_DOMAINS,
                               contents={"highlights": True})
            except Exception as e:
                logger.warning(f"[BuyerIntent] Exa query failed: {e}")
                continue
            for x in r.results:
                hl = " ".join(getattr(x, "highlights", []) or [])
                posts.append({
                    "title": x.title or "",
                    "text": hl[:2000],
                    "url": x.url or "",
                    "author": getattr(x, "author", "") or "",
                    "platform": "exa",
                    "posted_at": getattr(x, "published_date", "") or "",
                    "query": pain,
                })
        logger.info(f"[BuyerIntent] Exa: {len(posts)} results")
    except Exception as e:
        logger.error(f"[BuyerIntent] Exa failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Source: Apify — LinkedIn posts (real post content past the login wall)
# ---------------------------------------------------------------------------

async def search_apify_linkedin(queries: list[str]) -> list[dict]:
    if not APIFY_API_TOKEN:
        return []
    posts = []
    try:
        async with httpx.AsyncClient(timeout=90) as http:
            for query in queries[:2]:  # actor runs are slow/billed — limit
                # run-sync-get-dataset-items: starts actor, waits, returns items in one call
                url = f"https://api.apify.com/v2/acts/{APIFY_LINKEDIN_ACTOR.replace('/', '~')}/run-sync-get-dataset-items"
                resp = await http.post(
                    url,
                    params={"token": APIFY_API_TOKEN},
                    json={"keywords": query, "maxItems": 15, "sortType": "date_posted"},
                )
                if resp.status_code not in (200, 201):
                    logger.warning(f"[BuyerIntent] Apify {resp.status_code}: {resp.text[:120]}")
                    continue
                for item in resp.json():
                    text = item.get("text") or item.get("postText") or item.get("content") or ""
                    posts.append({
                        "title": (text[:80] + "...") if text else "LinkedIn post",
                        "text": text[:2000],
                        "url": item.get("url") or item.get("postUrl") or "",
                        "author": item.get("authorName") or item.get("author", "") or "",
                        "platform": "linkedin",
                        "posted_at": item.get("postedAt") or item.get("date", ""),
                        "query": query,
                    })
        logger.info(f"[BuyerIntent] Apify LinkedIn: {len(posts)} posts")
    except Exception as e:
        logger.error(f"[BuyerIntent] Apify failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Source 4: Reddit via Serper (works without PRAW creds)
# ---------------------------------------------------------------------------

async def search_reddit_via_serper(queries: list[str]) -> list[dict]:
    """Google-indexed Reddit posts — no Reddit API needed.

    IMPORTANT: the free Serper tier REJECTS `site:` operators ("Query pattern not
    allowed for free accounts"), so the old `site:reddit.com {q}` form silently
    returned 0 — Reddit was effectively dead. Fix: append "reddit" as a plain keyword
    and filter the organic results down to reddit.com links. Works on free Serper.
    (PRAW is the only direct path but it's gated behind Reddit's Data-API registration;
    Reddit's public .json now 403s unauthenticated — so this is the free route.)

    Runs up to 8 buyer-voice queries concurrently to keep wall-clock down."""
    if not SERPER_API_KEY:
        return []
    posts = []
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            async def one_query(query: str) -> list[dict]:
                out = []
                resp = await http.post(
                    SERPER_SEARCH_URL,
                    headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                    json={"q": f"{query} reddit", "num": 20, "tbs": "qdr:m"},
                )
                usage.log_serper()
                if resp.status_code != 200:
                    return out
                for r in resp.json().get("organic", []):
                    link = r.get("link", "")
                    if "reddit.com" not in link:
                        continue  # keep only Reddit results
                    out.append({
                        "title": r.get("title", ""),
                        "text": r.get("snippet", "")[:2000],
                        "url": link,
                        "author": "",
                        "platform": "reddit",
                        "posted_at": "",
                        "query": query,
                    })
                return out

            # Run up to 8 buyer-voice queries concurrently (was 3 sequential)
            batches = await asyncio.gather(*[one_query(q) for q in queries[:8]])
            for b in batches:
                posts.extend(b)
        logger.info(f"[BuyerIntent] Reddit (via Serper): {len(posts)} posts")
    except Exception as e:
        logger.error(f"[BuyerIntent] Reddit-Serper failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Pre-filter + dedup
# ---------------------------------------------------------------------------

def prefilter_posts(posts: list[dict]) -> list[dict]:
    """Keep posts that show buyer intent.
    Exa results are ALREADY semantically filtered — trust them, skip the keyword
    regex (applying it would defeat semantic search). Keyword sources
    (Reddit/LinkedIn via Serper) still get the regex to cut noise."""
    seen_urls = set()
    passed = []
    for p in posts:
        url = p.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if p.get("platform") == "exa":
            passed.append(p)  # semantic match already done by Exa
            continue
        combined = f"{p.get('title', '')} {p.get('text', '')}"
        if BUYER_REGEX.search(combined):
            passed.append(p)
    logger.info(f"[BuyerIntent] Pre-filter: {len(passed)}/{len(posts)} passed (exa bypassed regex)")
    return passed


def make_signal_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

async def run(profile_id: str = None) -> dict:
    """
    Harvest buyer-intent posts from all sources, filter, push to signal queue.
    Vector match + scoring happen downstream (same pipeline as funding).
    """
    logger.info(f"[BuyerIntent] Starting run (profile_id={profile_id})")

    # 1. Load ICPs to build queries from
    icps = []
    try:
        q = supabase.table("user_profiles").select("id, icp_text").eq("is_active", True)
        if profile_id:
            q = q.eq("id", profile_id)
        result = q.not_.is_("icp_text", "null").execute()
        icps = [p["icp_text"] for p in (result.data or []) if p.get("icp_text")]
    except Exception as e:
        logger.error(f"[BuyerIntent] Failed to load ICPs: {e}")

    if not icps:
        return {"signals_queued": 0, "error": "no active ICPs"}

    # 2. Buyer-language queries — search_profile facets first, Haiku fallback
    from app.pipeline.query_builder import load_search_profile, buyer_queries, filter_by_performance
    all_queries = []
    sp = load_search_profile(profile_id) if profile_id else None
    if sp and sp.get("buyer_pain_phrases"):
        base = buyer_queries(sp)
        filtered_q = filter_by_performance(base, profile_id, "buyer_intent")
        # never let the perf filter empty the set — keep base if it nuked everything
        all_queries = filtered_q if filtered_q else base
        logger.info(f"[BuyerIntent] Using {len(all_queries)} facet queries")
    if not all_queries:
        # fallback: generate from ICP with Haiku
        for icp in icps:
            all_queries.extend(await build_buyer_queries(icp))
    all_queries = list(dict.fromkeys(all_queries))  # dedupe, keep order
    logger.info(f"[BuyerIntent] {len(all_queries)} buyer queries: {all_queries}")

    if not all_queries:
        return {"signals_queued": 0, "error": "query generation failed"}

    # Build ONE rich, context-loaded Exa query from the full ICP (uses all our context,
    # not just phrases) — this is Exa's strength.
    service_desc = ""
    try:
        prow = supabase.table("user_profiles").select("service_description").eq("id", profile_id).execute()
        service_desc = (prow.data[0].get("service_description") or "") if prow.data else ""
    except Exception:
        pass
    rich_query = build_exa_rich_query(icps[0], service_desc) if profile_id else ""
    if rich_query:
        logger.info(f"[BuyerIntent] Exa rich query: {rich_query[:160]}")

    # 3. Harvest from all sources
    posts = []
    posts += search_reddit(all_queries)              # PRAW (if creds set)
    if not HAS_PRAW:
        posts += await search_reddit_via_serper(all_queries)  # fallback, works today
    posts += await search_hn(all_queries)
    posts += await search_linkedin_posts(all_queries)         # Serper LinkedIn snippets
    posts += await search_exa(all_queries, rich_query=rich_query)  # semantic, rich context
    # Apify LinkedIn posts disabled — actor returns 403 + Apify parked on cost.
    # posts += await search_apify_linkedin(all_queries)
    logger.info(f"[BuyerIntent] {len(posts)} total posts harvested")

    # 4. Pre-filter for buyer language
    filtered = prefilter_posts(posts)

    # 5. Push to signal queue (vector match + scoring downstream)
    queued = 0
    for p in filtered:
        signal = {
            "signal_hash":     make_signal_hash(p["url"]),
            "signal_type":     "buyer_intent",
            "company_name":    None,  # extracted during scoring
            "company_domain":  None,
            "raw_text":        f"{p['title']}. {p['text']}"[:2000],
            "source_url":      p["url"],
            "source_platform": p["platform"],
            "funding_amount":  None,
            "funding_round":   None,
            "summary":         p["title"],
            "source_query":    p.get("query", ""),
        }
        await signal_queue.push(signal)
        queued += 1

    logger.info(f"[BuyerIntent] Done — {len(posts)} harvested → {len(filtered)} buyer language → {queued} queued")
    return {
        "harvested":     len(posts),
        "buyer_language": len(filtered),
        "signals_queued": queued,
        "sources": {
            "reddit":     "praw" if HAS_PRAW else ("serper" if SERPER_API_KEY else False),
            "hackernews": True,
            "linkedin":   "apify" if APIFY_API_TOKEN else ("serper" if SERPER_API_KEY else False),
            "exa":        bool(EXA_API_KEY),
        },
        "_queries": all_queries,
    }
