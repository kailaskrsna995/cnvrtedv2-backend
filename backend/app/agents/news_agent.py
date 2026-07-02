"""
NEWS AGENT — trigger events beyond funding
==========================================
A company changing direction needs execution help. Detects:
  - Product/feature launches
  - New exec hires (CMO, Head of Content, VP Marketing...)
  - Market expansion announcements
  - Rebrands, partnerships, user milestones

Same optimised pipeline as funding agent:
  Serper News → regex pre-filter → batch Haiku extraction → signal queue
Downstream (leads_v2) does vector match + scoring.
"""

import re
import json
import hashlib
import httpx
import logging
from datetime import datetime, timezone, timedelta
from app.llm import Anthropic
from app.config import SERPER_API_KEY, ANTHROPIC_API_KEY
from app.queue import signal_queue
from app.database import supabase

logger = logging.getLogger(__name__)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

SERPER_URL = "https://google.serper.dev/news"

GLOBAL_QUERIES = [
    "startup launches video platform 2026",
    "company appoints new CMO 2026",
    "startup appoints head of content 2026",
    "company announces rebrand 2026",
    "startup expands to US market 2026",
    "company reaches 1 million users 2026",
    "startup launches new product 2026",
]

# Article must contain a trigger-event word
TRIGGER_REGEX = re.compile(
    r'\b(launch(es|ed)?|appoint(s|ed)?|hires?|joins? as|named|rebrand|'
    r'expand(s|ing|ed)?|enters?|partnership|partners with|milestone|'
    r'reaches|surpasses|unveil(s|ed)?|introduc(es|ed)|debuts?)\b',
    re.IGNORECASE
)


def _is_recent(article: dict, max_days: int = 60) -> bool:
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


async def generate_icp_queries(icp_text: str) -> list[str]:
    """Trigger-event queries specific to this ICP's industry."""
    prompt = f"""An agency targets this ideal customer:
{icp_text[:1200]}

Generate 4 Google News queries to find TRIGGER EVENTS at companies matching
this ICP. Trigger events: product launches, new marketing/content executives,
market expansion, rebrands, growth milestones. NOT funding (covered elsewhere).

Each query: under 8 words, include industry keywords from the ICP, end with 2026.
Return ONLY a JSON array of 4 strings."""
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
        logger.warning(f"[NewsAgent] ICP query gen failed: {e}")
        return []


async def search_serper_news(query: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                SERPER_URL,
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": 10, "gl": "us", "hl": "en", "tbs": "qdr:m2"},
            )
            if resp.status_code == 200:
                articles = resp.json().get("news", [])
                return [a for a in articles if _is_recent(a)]
            return []
    except Exception as e:
        logger.error(f"[NewsAgent] Serper failed: {e}")
        return []


BATCH_EXTRACT_PROMPT = """Extract trigger-event information from these news articles.
A trigger event = product launch, new executive hire, expansion, rebrand,
partnership, or growth milestone at a company.

Articles:
{articles}

Return ONLY a valid JSON array, one object per article in order, no markdown:
[
  {{
    "is_trigger_event": true,
    "company_name": "exact name or null",
    "company_domain": "domain.com or null",
    "event_type": "launch/exec_hire/expansion/rebrand/partnership/milestone",
    "summary": "one sentence: what happened and why it creates a need"
  }},
  ...
]"""


async def batch_extract(articles: list[dict]) -> list[dict]:
    """Extract trigger events, 5 articles per Haiku call."""
    results = []
    for i in range(0, len(articles), 5):
        batch = articles[i:i + 5]
        articles_text = "\n\n".join([
            f"Article {j+1}:\nTitle: {a.get('title', '')}\nSnippet: {a.get('snippet', '')}"
            for j, a in enumerate(batch)
        ])
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=700,
                messages=[{"role": "user", "content": BATCH_EXTRACT_PROMPT.format(articles=articles_text)}]
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            parsed = json.loads(raw)
            for j, a in enumerate(batch):
                if j < len(parsed) and parsed[j].get("is_trigger_event") and parsed[j].get("company_name"):
                    results.append({"article": a, "extracted": parsed[j]})
        except Exception as e:
            logger.warning(f"[NewsAgent] Batch extract failed: {e}")
    return results


def make_signal_hash(company: str, url: str) -> str:
    return hashlib.sha256(f"{company}{url}".encode()).hexdigest()


async def run(profile_id: str = None) -> dict:
    logger.info(f"[NewsAgent] Starting run (profile_id={profile_id})")

    # 1. Queries: Seller Brain dossier first (precise), then facets. Drop the generic
    # GLOBAL_QUERIES when a dossier exists — they pull off-vertical launches/CMO-hires.
    from app.pipeline.query_builder import (
        load_search_profile, news_queries, filter_by_performance, dossier_queries,
    )
    queries = list(GLOBAL_QUERIES)
    sp = load_search_profile(profile_id) if profile_id else None
    if sp:
        dossier = sp.get("dossier")
        facet_queries = news_queries(sp)
        if dossier:
            dq = await dossier_queries(dossier, "news")
            queries = dq + facet_queries if dq else facet_queries
        else:
            queries.extend(facet_queries)
        logger.info(f"[NewsAgent] {len(queries)} queries (dossier={'yes' if dossier else 'no'})")
        queries = filter_by_performance(queries, profile_id, "news")
    else:
        try:
            q = supabase.table("user_profiles").select("id, icp_text").eq("is_active", True)
            if profile_id:
                q = q.eq("id", profile_id)
            result = q.not_.is_("icp_text", "null").execute()
            for p in (result.data or []):
                if p.get("icp_text"):
                    queries.extend(await generate_icp_queries(p["icp_text"]))
        except Exception as e:
            logger.warning(f"[NewsAgent] ICP queries failed: {e}")

    # 2. Search (tagged with source query)
    all_articles = []
    for query in queries:
        articles = await search_serper_news(query)
        for a in articles:
            a["_query"] = query
        all_articles.extend(articles)

    # Dedup by URL
    seen = set()
    unique = []
    for a in all_articles:
        url = a.get("link", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(a)

    # 3. Regex pre-filter (free)
    filtered = [a for a in unique if TRIGGER_REGEX.search(f"{a.get('title','')} {a.get('snippet','')}")]
    logger.info(f"[NewsAgent] {len(unique)} articles → {len(filtered)} passed trigger regex")

    # 4. Batch Haiku extraction
    extracted = await batch_extract(filtered)

    # 5. Queue signals
    queued = 0
    seen_hashes = set()
    for item in extracted:
        ext, article = item["extracted"], item["article"]
        company = ext.get("company_name") or ""
        url = article.get("link", "")
        # Drop misaligned extractions (batch-by-position can staple a name to the wrong article)
        from app.agents.funding_agent import name_matches_article
        if not name_matches_article(company, article.get("title", ""), article.get("snippet", "")):
            logger.info(f"[NewsAgent] dropped misaligned extraction: '{company}' not in article")
            continue
        h = make_signal_hash(company, url)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        await signal_queue.push({
            "signal_hash":     h,
            "signal_type":     "news",
            "company_name":    company,
            "company_domain":  ext.get("company_domain"),
            "raw_text":        f"{article.get('title','')}. {article.get('snippet','')}",
            "source_url":      url,
            "source_platform": "serper_news",
            "funding_amount":  None,
            "funding_round":   ext.get("event_type"),  # reuse column for event type
            "summary":         ext.get("summary", article.get("title", "")),
            "source_query":    article.get("_query", ""),
        })
        queued += 1

    logger.info(f"[NewsAgent] Done — {queued} trigger events queued")
    return {"articles": len(unique), "passed_regex": len(filtered), "queued": queued,
            "_queries": queries}
