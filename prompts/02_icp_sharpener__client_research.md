# Box ② ICP Sharpener — researches the real clients, re-derives an evidence-weighted ICP

## SHARPEN_PROMPT (verbatim constant)

```
You are sharpening a seller's ICP. The CURRENT ICP was auto-derived from their
marketing homepage, so it is likely TOO BROAD and flat ("we serve anyone who needs X"). Your job is
to RE-DERIVE a sharper, EVIDENCE-WEIGHTED ICP grounded in WHO THE SELLER ACTUALLY SERVES.

THE KEY EVIDENCE — the seller's real clients and what each one actually is:
{client_research}

Current (too-broad) ICP:
{icp_text}

Seller context (what they offer + how they deliver):
{user_context}

Re-derive the ICP with these rules:
1. Identify the COMMON PATTERN across the real clients at the SECTOR + SITUATION level — what kind
   of business they are AND the job they hire this seller for. Then generalize to the high-value
   CORE, which is a SECTOR + a SITUATION, NOT the narrowest taxonomic label of one or two clients.
   CRITICAL — do NOT over-narrow: if some clients are (say) audio apps, the core is still the broader
   "media & entertainment companies scaling high-volume video content" — which INCLUDES adjacent
   media formats with the same situation: audio platforms adapting IP to video, microdrama /
   vertical-video apps, regional OTT/streaming, and AI-media studios. The best leads are companies
   in these ADJACENT formats, not only the clients' exact category. Be concrete but inclusive.
2. State this core FIRST and most prominently — it is where the best leads live. Then list SECONDARY
   segments (adjacent fits) clearly marked as secondary.
3. Name the high-value TRIGGERS for the core (e.g. raised funding to scale content, acquired a
   studio, launching a slate, expanding to new formats/markets).
4. Keep it specific and outbound-usable. No fluff. ~180-260 words.

Return ONLY the rewritten ICP text (no preamble, no markdown headers).
```

## research_clients() — Serper lookup of each client (verbatim source)

```python
async def research_clients(client_names: list[str]) -> str:
    """Look up what each named client/lookalike ACTUALLY is (via Serper).
    A homepage says "we serve everyone"; the real ICP is evidenced by WHO they serve.
    Returns a short digest: 'Pocket FM — audio series/microdrama streaming app, India ...'.
    """
    if not client_names or not SERPER_API_KEY:
        return ""
    digest = []
    async with httpx.AsyncClient(timeout=15) as http:
        async def one(name: str) -> str:
            try:
                r = await http.post(
                    SERPER_SEARCH_URL,
                    headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                    json={"q": f"{name} company what they do", "num": 3},
                )
                if r.status_code != 200:
                    return f"- {name}: (lookup failed)"
                data = r.json()
                bits = []
                kg = data.get("knowledgeGraph") or {}
                if kg.get("description"):
                    bits.append(kg["description"])
                if kg.get("type"):
                    bits.append(f"({kg['type']})")
                for o in (data.get("organic") or [])[:2]:
                    bits.append(o.get("snippet", ""))
                blurb = " ".join(b for b in bits if b)[:350]
                return f"- {name}: {blurb}"
            except Exception as e:
                return f"- {name}: (error {e})"
        import asyncio
        rows = await asyncio.gather(*[one(n) for n in client_names[:6]])
    return "\n".join(rows)

```

## sharpen_icp_from_clients() — orchestration (verbatim source)

```python
async def sharpen_icp_from_clients(profile_id: str) -> dict:
    """Research the profile's named clients/lookalikes, then re-derive a sharper, evidence-weighted
    ICP (core sweet-spot first). Re-stores icp_text + icp_vector + search_profile. Returns before/after.
    """
    p = supabase.table("user_profiles").select("icp_text, user_context, search_profile") \
        .eq("id", profile_id).execute()
    if not p.data:
        return {"error": "profile not found"}
    row = p.data[0]
    old_icp = row.get("icp_text", "") or ""
    user_context = row.get("user_context", "") or ""
    sp = row.get("search_profile") or {}
    clients = sp.get("lookalike_companies") or []
    if not clients:
        return {"error": "no clients/lookalikes on profile to research"}

    research = await research_clients(clients)
    logger.info(f"[ProfileAgent] client research:\n{research}")

    prompt = SHARPEN_PROMPT.format(
        client_research=research, icp_text=old_icp[:1800], user_context=user_context[:1500])
    resp = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=900,
        messages=[{"role": "user", "content": prompt}])
    new_icp = resp.content[0].text.strip()

    # Re-embed + rebuild search facets off the sharpened ICP
    icp_vector = await generate_icp_vector(new_icp)
    search_profile = await build_search_profile(new_icp, user_context)
    supabase.table("user_profiles").update({
        "icp_text": new_icp,
        "icp_vector": icp_vector,
        "search_profile": search_profile,
    }).eq("id", profile_id).execute()

    return {
        "clients_researched": clients,
        "research": research,
        "old_icp": old_icp,
        "new_icp": new_icp,
        "search_profile": search_profile,
    }

```
