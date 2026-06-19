# Box Ⓐ Funding Agent

## BATCH_EXTRACT_PROMPT (Haiku extraction, verbatim constant)

```
Extract funding information from these news articles.

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
]
```

## GLOBAL_QUERIES (vertical-agnostic safety net)

```
startup raises funding 2026
company closes funding round 2026
startup raises funding India 2026
```

## generate_icp_queries() — Haiku query-gen fallback (verbatim source)

```python
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

```
