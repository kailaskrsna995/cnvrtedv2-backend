# Box Ⓒ News Agent

## BATCH_EXTRACT_PROMPT (trigger-event extraction, verbatim constant)

```
Extract trigger-event information from these news articles.
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
]
```

## GLOBAL_QUERIES

```
startup launches video platform 2026
company appoints new CMO 2026
startup appoints head of content 2026
company announces rebrand 2026
startup expands to US market 2026
company reaches 1 million users 2026
startup launches new product 2026
```

## generate_icp_queries() — query-gen (verbatim source)

```python
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

```
