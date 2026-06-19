# Box ⑫ Outreach Opener (Sonnet)

## generate_outreach() — contains the full opener prompt (verbatim source)

```python
async def generate_outreach(leads: list[dict], user_context: str, icp_text: str) -> list[dict]:
    """
    Personalized first-line opener per lead — ONE batched Sonnet call for the whole
    list (cheap, fast, no per-lead concurrency). Each opener references THIS lead's
    actual trigger/proof in the seller's voice. Mutates leads in place (adds "outreach").

    Safe by design: on any failure leads are returned unchanged (no "outreach" key),
    so a flaky call never blocks the run or the rest of the pipeline.
    """
    if not leads:
        return leads

    items = []
    for i, l in enumerate(leads):
        ev = l.get("evidence_type", "trigger")
        items.append(
            f"{i}. {l.get('company_name') or '(no name)'} [{ev}] — "
            f"proof: \"{(l.get('proof') or l.get('summary') or '')[:220]}\" "
            f"| why: {(l.get('why') or '')[:160]}"
        )
    listing = "\n".join(items)

    prompt = f"""You write the FIRST LINE of a cold outreach message — the opener that proves
the sender did their homework. You are writing AS the seller below, to each prospect.

SELLER (who is reaching out, what they offer):
{user_context[:1500]}

SELLER'S IDEAL CUSTOMER:
{icp_text[:1200]}

For EACH prospect, write ONE personalized opening line (max ~25 words) that:
- references THIS prospect's specific trigger/intent (use the proof — the real event or ask)
- sounds human and specific, never generic ("I came across..." / "Hope you're well" = banned)
- for a "stated_intent" lead: respond directly to what they asked for
- for a "trigger" lead: connect the event to why the seller can help NOW
- does NOT pitch hard or list features — it earns the next sentence, nothing more
- no greeting, no "my name is", no sign-off — just the opening line itself

Prospects:
{listing}

Return ONLY a JSON array, one object per prospect IN ORDER:
[{{"i": 0, "outreach": "the opening line"}}, ...]"""

    try:
        resp = await client.messages.create(
            model=SONNET_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        openers = json.loads(raw)
    except Exception as e:
        logger.warning(f"[Outreach] failed, leaving leads without openers: {e}")
        return leads

    omap = {o.get("i"): o.get("outreach", "") for o in openers if isinstance(o, dict)}
    for i, l in enumerate(leads):
        line = (omap.get(i) or "").strip()
        if line:
            l["outreach"] = line
    logger.info(f"[Outreach] wrote {sum(1 for l in leads if l.get('outreach'))}/{len(leads)} openers")
    return leads

```
