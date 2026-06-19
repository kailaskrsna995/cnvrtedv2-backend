# Box ④ Build Search Profile — classifies delivery model + builds search facets

## build_search_profile() — contains the full prompt (verbatim source)

```python
async def build_search_profile(icp_text: str, user_context: str) -> dict:
    """
    Decompose ICP + UserContext into structured search facets — done ONCE,
    stored on the profile. Agents build queries from these deterministically
    instead of regenerating blind guesses every run.
    """
    import json
    prompt = f"""Decompose this SELLER's ideal customer profile into web-search facets used to FIND in-market buyers.

STEP 1 — Read the seller context and classify HOW the seller delivers what they offer.
This is critical: it determines how a buyer would phrase their search.
- "self_serve_product"  — a tool/app/software/SaaS/platform the buyer uses THEMSELVES to do the work
  (e.g. an AI video generator, a design tool). The buyer is shopping for a TOOL, not a person.
- "service_or_agency"   — an agency/studio/freelancer/done-for-you service. The buyer wants to HIRE someone to do the work.
- "marketplace_platform"— connects buyers with providers/supply.
Pick the SINGLE best fit from the seller context below.

STEP 2 — Generate buyer_pain_phrases that match HOW a real buyer seeking THAT KIND of seller types/posts.
The MODALITY must match the seller's delivery model — this is the most common mistake, do not get it wrong:
- self_serve_product → buyer wants a TOOL to do it themselves / faster / cheaper:
    GOOD: "best AI video generator", "tool to make product videos", "how to make a game trailer without an editor",
          "app to create UGC ads", "automate video editing", "software to animate images"
    BAD (these seek a HUMAN — wrong for a product): "looking for a video editor", "hiring someone to make videos"
- service_or_agency → buyer wants to HIRE a person/agency:
    GOOD: "looking for a video editor", "hire an animation studio", "recommend a trailer editor", "need someone to make game trailers"
    BAD (these seek software — wrong for an agency): "best video editing software", "AI tool to make videos"
- marketplace_platform → phrase for whichever side the seller monetises.
ALWAYS casual first-person seeker voice, never marketing jargon. BAD always: "creative asset bottleneck", "content production at scale".

ICP:
{icp_text[:1500]}

Seller context (what they sell AND how they deliver it — read carefully to classify):
{user_context[:1500]}

Return ONLY valid JSON, no markdown:
{{
  "seller_delivery_model": "self_serve_product | service_or_agency | marketplace_platform",
  "industry_terms": ["4-6 core industry/product terms buyers' companies use, e.g. 'podcast platform', 'audio app'"],
  "adjacent_terms": ["3-4 adjacent/synonym terms that widen coverage, e.g. 'creator economy', 'spoken audio'"],
  "buyer_pain_phrases": ["5 short phrases EXACTLY as a real buyer would TYPE or POST when actively seeking this seller — casual first-person seeker voice, MODALITY MATCHING the seller_delivery_model above (tool-seeking for a product, hire-seeking for an agency)."],
  "lookalike_companies": ["known clients or example companies from the context — used to find similar companies"],
  "geo_terms": ["geographies mentioned or implied, e.g. 'India', 'US' — empty list if global"],
  "stage_terms": ["company stages that fit, e.g. 'seed', 'Series A', 'growth stage'"]
}}"""
    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        profile = json.loads(raw.strip())
        logger.info(f"[ProfileAgent] search_profile built: { {k: len(v) for k, v in profile.items()} }")
        return profile
    except Exception as e:
        logger.error(f"[ProfileAgent] search_profile build failed: {e}")
        return {}

```
