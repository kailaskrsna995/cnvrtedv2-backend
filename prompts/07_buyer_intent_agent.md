# Box Ⓑ Buyer-Intent Agent

## build_buyer_queries() — Haiku buyer-phrase gen (verbatim source)

```python
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

```

## build_exa_rich_query() — rich semantic query (verbatim source)

```python
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

```

## search_reddit_via_serper() — free Reddit path (verbatim source)

```python
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

```

## prefilter_posts() — buyer-language filter (verbatim source)

```python
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

```
