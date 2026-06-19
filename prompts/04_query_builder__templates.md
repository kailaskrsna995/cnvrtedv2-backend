# Box ⑤ Query Builder — deterministic search-query templates (no LLM)

## FUNDING_TEMPLATES

```
{term} raises funding 2026
{term} raises Series A 2026
{term} startup seed round 2026
```

## FUNDING_TRIGGER_TEMPLATES (content-scaling / acquisition triggers)

```
{term} raises funding to scale content 2026
{term} acquires production studio 2026
{term} expands into video 2026
```

## NEWS_TEMPLATES

```
{term} launches product 2026
{term} appoints CMO 2026
{term} company expansion 2026
```

## funding_queries() — how templates combine (verbatim source)

```python
def funding_queries(sp: dict, max_queries: int = 16) -> list[str]:
    """industry/adjacent terms × funding templates + content-scaling trigger queries
    + geo variants + lookalikes."""
    queries = []
    terms = _terms(sp)
    for term in terms:
        for tpl in FUNDING_TEMPLATES:
            queries.append(tpl.format(term=term))
    # Content-scaling / acquisition TRIGGER queries on the top terms — these surface the
    # high-value "raised/acquired to scale video" signal (the strongest funding lead).
    for term in terms[:3]:
        for tpl in FUNDING_TRIGGER_TEMPLATES:
            queries.append(tpl.format(term=term))
    # Geo-flavoured variants for first couple of terms
    for geo in (sp.get("geo_terms") or [])[:2]:
        for term in terms[:2]:
            queries.append(f"{term} raises funding {geo} 2026")
    # Lookalike-driven: find funding news about competitors of known clients
    for company in (sp.get("lookalike_companies") or [])[:3]:
        queries.append(f"{company} competitor raises funding 2026")
    return list(dict.fromkeys(queries))[:max_queries]

```
