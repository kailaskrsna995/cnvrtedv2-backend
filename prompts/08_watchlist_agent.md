# Box Ⓓ Watchlist Agent (Target List)

## BUILD_PROMPT (target-account list builder, verbatim constant)

```
You are building a high-quality target-account list for this seller.

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
[{{"company_name": "...", "company_domain": "domain.com or null", "reason": "one short line why it fits"}}, ...]
```

## _DELIVERY_LINES (delivery-model framing, verbatim constant)

```
{
  "service_or_agency": "This seller is a SERVICE/STUDIO/AGENCY (done-for-you). Target companies that PRODUCE high volumes of video/marketing/content and would realistically OUTSOURCE that production to an external studio \u2014 e.g. media/streaming/OTT/audio platforms, regional content players, publishers, and content-heavy growth-stage D2C brands. NOT companies with large in-house studios that never outsource.",
  "self_serve_product": "This seller is a SELF-SERVE PRODUCT/TOOL. Target companies/teams that would realistically ADOPT a self-serve or mid-market tool to make content themselves \u2014 growth-stage brands, studios, agencies, and creator-led teams with hands-on production needs.",
  "marketplace_platform": "This seller is a MARKETPLACE/PLATFORM. Target companies on the side the seller monetises that would join or transact on such a platform."
}
```

## _validate_candidates() — Sonnet filter (verbatim source)

```python
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

```

## check_company() — per-company trigger query (verbatim source)

```python
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

```
