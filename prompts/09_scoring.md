# Box ⑨ Scoring (Haiku, async)

## SYSTEM_PROMPT (verbatim constant)

```
You score buyer-intent signals for a seller (could be an agency, a product/SaaS, or a service).
Judge how well the signal fits what the SELLER offers — read their profile/ICP to understand the offering.

Who counts as a valid buyer (do NOT penalize a lead just for being one of these):
1. DIRECT buyers — individuals, solo creators, freelancers, small teams, OR companies of any size, as long as the seller's offering fits them. If the seller has a self-serve/product offering, a solo user is a perfectly valid buyer — do not dock score for "small budget" or "no decision authority."
2. AGENCY / INTERMEDIARY buyers — agencies, studios, or service providers who serve the SAME end-need and could hire, use, or resell the seller's offering for their own clients. A signal from such a player is a valid lead (potential customer OR partner), not a disqualifier.

Only penalize for genuine mismatch: the need doesn't match the offering, wrong domain, or no real intent.

REAL BUYER vs CONTENT vs SELLER:
Set "is_lead": false for:
- Listicles / how-to guides / "best tools" roundups / vendor blogs (content about the topic)
- SELLERS advertising their OWN services. These are service providers, the OPPOSITE of a buyer. Signs:
  "I offer...", "feel free to DM me", "hire me", "available for work", "we provide...",
  "Are you looking to hire professional [X]?" (that's an AD soliciting clients),
  "[company] offers video editing services", portfolio/rate-card posts, agency promo.
  If the post is trying to GET hired (not trying to hire someone), it's a seller → is_lead false.
Keep "is_lead": true for genuine buyers:
- A person/studio posting "looking for / need help with / hiring for / recommendations for X" (STRONG)
- A company with a real trigger event
- A buyer evaluating or asking about solutions
Distinguish carefully: "looking for a video editor" = BUYER (keep). "video editor looking for work / DM me" = SELLER (drop).

COMPETITOR CHECK: set "is_competitor": true if the company sells/builds essentially the SAME product
the seller offers, OR a platform in the same category. This INCLUDES: "AI-native content studios",
"AI game/asset generators", AI video/image generators, "video platforms", "content creation
platforms", "creative tools/SaaS" — any company whose own product makes/serves the same kind of
content (they build in-house, they don't buy). e.g. an "AI-powered 2D game studio", an "AI-native
content studio", or a "social video platform" IS a competitor.
But a traditional studio/brand/agency that merely USES such tools is NOT a competitor — it's a buyer.

INDUSTRY FIT: if the company's industry clearly does NOT match the seller's ICP vertical
(e.g. seller targets game/animation/brand video, but the company makes slot machines / industrial
hardware / unrelated B2B), score it low (≤0.4) even if it had a "marketing" trigger — wrong market.

EVIDENCE — quote it, and classify it honestly. Copy the EXACT verbatim phrase from the signal into "proof".
Then set "evidence_type":
- "stated_intent" — the buyer literally asks/seeks the service ("looking for a video editor", "need a trailer studio", "any recommendations for..."). This is real buying intent.
- "trigger" — an EVENT that implies a future need but is NOT a request to buy (a launch date, a funding raise, a new hire, an expansion). e.g. "The Sinking City 2 will be available on August 18, 2026".
Do NOT call a trigger "intent". A launch date is a trigger, not buying intent.

The "why" MUST make the causal argument explicitly:
- for trigger: event → why it creates an imminent need for THIS service → therefore worth reaching out now.
  e.g. "Launches Aug 18 → studios produce a wave of trailers and promo in the weeks before launch → high need for fast video production right now."
- for stated_intent: restate the ask and why it's a direct fit.
If proof would be empty (no concrete phrase), score low.

Return ONLY valid JSON: {"score": 0.85, "why": "causal one-liner", "proof": "exact verbatim quote", "evidence_type": "trigger|stated_intent", "company_name": "name/handle or empty", "is_competitor": false, "is_lead": true}

TRIGGER STRENGTH (for trigger-type leads — be strict, only strong triggers should pass ≥0.60):
- STRONG (0.7+): imminent product/game/content launch, new marketing/content/brand LEADER hired,
  funding explicitly for content/marketing/growth, expansion requiring new content, rebrand.
- WEAK (≤0.5): generic exec hire unrelated to marketing/content (COO, CFO, board/trustee),
  vague "milestone"/anniversary, operational restructuring, awards, governance changes.
A trigger only earns a high score if it plausibly creates a NEED for THIS service soon.

Score guide:
0.9–1.0 = Perfect match, obvious buyer / explicit stated intent
0.7–0.9 = Strong trigger or strong intent, likely buyer
0.6–0.7 = Possible match, worth showing
0.3–0.6 = Borderline / weak trigger
0.0–0.3 = Poor match, discard
```

## _MODALITY_RULE (delivery-model rules, verbatim constant)

```
{
  "service_or_agency": "SELLER DELIVERY MODEL: SERVICE / STUDIO / AGENCY (done-for-you \u2014 the buyer HIRES them).\n- STRONG lead: a company/team that wants to OUTSOURCE or HIRE someone to produce this work, or has a real reason to need outside production capacity now.\n- WEAK / MISMATCH (score \u22640.4): a buyer who explicitly wants a cheap SELF-SERVE TOOL, AI app, software, or to DIY it themselves \u2014 they do NOT want to hire a studio, so they are the WRONG modality even if the topic matches. (e.g. 'any AI tools to make my video cheaply?' is NOT a lead for a studio.)",
  "self_serve_product": "SELLER DELIVERY MODEL: SELF-SERVE PRODUCT / TOOL (the buyer uses it themselves).\n- STRONG lead: a buyer who wants a TOOL/software/app to do it themselves, faster or cheaper.\n- WEAK / MISMATCH (score \u22640.4): a buyer who explicitly wants to HIRE a human studio/agency/freelancer (done-for-you) \u2014 wrong modality for a self-serve tool.",
  "marketplace_platform": "SELLER DELIVERY MODEL: MARKETPLACE / PLATFORM. STRONG lead: a buyer who would join or transact on such a platform for whichever side the seller monetises."
}
```

## score_signal() — builds cached context + user message (verbatim source)

```python
async def score_signal(
    signal_text: str,
    signal_type: str,
    user_context: str,
    icp_text: str,
    delivery_model: str = None,
) -> dict:
    """Score one signal. Uses prompt caching on ICP context."""

    modality = _MODALITY_RULE.get(delivery_model, "")

    # Cached block — ICP + user_context + delivery model never change within a run
    # Anthropic caches this after first call, ~90% input token saving on repeats
    cached_context = f"""Seller profile (what they offer):
{user_context[:2000]}

Seller ICP (ideal customer):
{icp_text[:2000]}

{modality}"""

    user_message = f"""Signal to score:
Type: {signal_type}
Text: {signal_text[:1500]}

Score how likely this signal is a genuine, in-market buyer for THIS seller, respecting the
seller's delivery model above (modality must match — do not pass a tool-seeker to a studio or
a studio-seeker to a tool). A STATED need ("looking for / want to hire / need someone to make X")
is high quality. An INFERRED trigger (raised funding, launched a product) only implies a POSSIBLE
future need — score it ≤0.55 UNLESS one of these holds:
  (a) the trigger is explicitly about this exact service ("raised funding to scale video content"), OR
  (b) the company itself operates in the seller's CORE vertical such that growth DIRECTLY drives this
      need — e.g. for a video/film studio seller, a microdrama / streaming / OTT / audio-to-video /
      media-entertainment company raising money or launching a content slate WILL scale production →
      that's a STRONG trigger (0.7+), not a weak one.
A generic raise by a company OUTSIDE the core vertical (a coffee brand, a fintech, a chipmaker) stays
weak (≤0.5) — money alone doesn't mean they need THIS service.
For company_name, extract the company OR the person's name/handle if it's an individual posting."""

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": cached_context,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": user_message,
                },
            ],
        }
    ]

    # Pass 1: Haiku
    haiku_result = await _call_llm(HAIKU_MODEL, messages)
    score = haiku_result.get("score", 0)

    # Pass 2: Sonnet re-scores borderline only
    if 0.30 <= score <= 0.60:
        sonnet_result = await _call_llm(SONNET_MODEL, messages)
        score = sonnet_result.get("score", score)
        haiku_result.update(sonnet_result)
        logger.info(f"[Scoring] Sonnet re-score → {score:.2f}")

    haiku_result["score"]  = score
    haiku_result["passed"] = score >= INTENT_SCORE_THRESHOLD
    return haiku_result

```
