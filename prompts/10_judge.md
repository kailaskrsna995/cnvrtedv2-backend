# Box ⑪ Final Judge (Sonnet)

## judge_leads() — contains the full judge prompt (verbatim source)

```python
async def judge_leads(leads: list[dict], user_context: str, icp_text: str,
                      delivery_model: str = None) -> dict:
    """
    Final strict gate — one Sonnet pass over all passing leads, judged against THIS
    seller's own ICP + offering (fully profile-driven, no hardcoded rules → universal).
    Sonnet follows instructions far better than Haiku's lenient self-grading.

    Returns {"keep": [leads...], "competitors": [names...]}.
    """
    if not leads:
        return {"keep": [], "competitors": []}

    items = []
    for i, l in enumerate(leads):
        items.append(f"{i}. {l.get('company_name') or '(no name)'} "
                     f"[{l.get('evidence_type','trigger')}] — proof: \"{(l.get('proof') or l.get('summary') or '')[:200]}\" "
                     f"| why: {(l.get('why') or '')[:200]}")
    listing = "\n".join(items)

    modality = _MODALITY_RULE.get(delivery_model, "")

    prompt = f"""You are the final quality gate for a lead list. Judge each candidate against
THIS specific seller — keep genuine, convertible, on-ICP buyers; cut clear misses. Be discerning but
NOT trigger-happy: cut the obvious junk (off-vertical, no signal, competitors, individuals with no
real intent), but KEEP every plausible on-ICP buyer. A healthy result is ~4-8 leads, not 1 — if you
are cutting almost everything, you are being too harsh. When genuinely unsure, KEEP.

DEFAULT TO KEEP for: any company squarely in the seller's CORE vertical with a real trigger or stated
need (e.g. for a media/video studio: a streaming/OTT/microdrama/audio/content company that raised,
launched a slate, or is hiring/seeking production) — these are exactly the target accounts, keep them
even if not perfectly worded.

SELLER (what they offer):
{user_context[:1800]}

SELLER'S IDEAL CUSTOMER:
{icp_text[:1800]}

{modality}

For EACH candidate decide:
- keep=true ONLY if it's a real, on-ICP, MODALITY-MATCHED buyer for THIS seller with a genuine
  reason-to-act-now — ideally a STATED need (they actually said they want this), or a trigger that
  is explicitly about this exact service.
- keep=false if the buyer wants the WRONG modality (wants a self-serve tool when the seller is a
  studio/agency, or wants to hire a studio when the seller is a self-serve tool).
- keep=false for INFERRED triggers with no stated need that are also OUTSIDE the core vertical:
  a coffee/fintech/D2C brand that just "raised money so they'll need content" is a guess, not intent.
  BUT keep a trigger if it explicitly ties to this service (raised to scale video/content) OR the
  company is squarely in the seller's CORE vertical where growth directly drives the need (a
  microdrama/streaming/OTT/media-entertainment company raising or launching a slate WILL need more
  video → keep). Core-vertical raises are leads; off-vertical raises are not.
- keep=false if: COMPETITOR, WRONG vertical/industry, vanity milestone, or no real buying signal.
- competitor=true if it sells/builds the same offering as the seller (set keep=false too).

Candidates:
{listing}

Return ONLY a JSON array, one object per candidate IN ORDER:
[{{"i": 0, "keep": true, "competitor": false, "reason": "short"}}, ...]"""

    try:
        resp = await client.messages.create(
            model=SONNET_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        verdicts = json.loads(raw)
    except Exception as e:
        logger.warning(f"[Judge] failed, keeping all leads: {e}")
        return {"keep": leads, "competitors": []}

    keep, competitors = [], []
    vmap = {v.get("i"): v for v in verdicts if isinstance(v, dict)}
    for i, l in enumerate(leads):
        v = vmap.get(i, {"keep": True})
        if v.get("competitor") and l.get("company_name"):
            competitors.append(l["company_name"])
        elif v.get("keep", True):
            keep.append(l)
    logger.info(f"[Judge] kept {len(keep)}/{len(leads)}, {len(competitors)} competitors flagged")
    return {"keep": keep, "competitors": competitors}

```
