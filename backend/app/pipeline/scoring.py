"""
SCORING ENGINE
==============
Two-pass scoring with prompt caching.

Pass 1: Haiku   — scores everything (cheap)
Pass 2: Sonnet  — re-scores borderline 0.30–0.60 only

Prompt caching: ICP + user_context are static per run.
Anthropic caches tokens >1024 — cuts input cost ~90% on repeated calls.
"""

import json
import logging
from app.llm import AsyncAnthropic
from app.config import ANTHROPIC_API_KEY, INTENT_SCORE_THRESHOLD, ANTHROPIC_MAX_RETRIES

logger = logging.getLogger(__name__)
# Async client — true concurrency + doesn't block the event loop (sync client
# made gather() fake-parallel and froze the server during scoring).
client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY, max_retries=ANTHROPIC_MAX_RETRIES)

HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You score buyer-intent signals for a seller (could be an agency, a product/SaaS, or a service).
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
  e.g. "Just raised a Series B to expand into new markets → an imminent build/launch push → high, immediate need for exactly what this seller provides — reach out now."
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
0.0–0.3 = Poor match, discard"""


_MODALITY_RULE = {
    "service_or_agency": (
        "SELLER DELIVERY MODEL: SERVICE / STUDIO / AGENCY (done-for-you — the buyer HIRES them).\n"
        "- STRONG lead: a company/team that wants to OUTSOURCE or HIRE someone to produce this work, "
        "or has a real reason to need outside production capacity now.\n"
        "- WEAK / MISMATCH (score ≤0.4): a buyer who explicitly wants a cheap SELF-SERVE TOOL, AI app, "
        "software, or to DIY it themselves — they do NOT want to hire a studio, so they are the WRONG "
        "modality even if the topic matches. (e.g. 'any AI tools to make my video cheaply?' is NOT a "
        "lead for a studio.)"
    ),
    "self_serve_product": (
        "SELLER DELIVERY MODEL: SELF-SERVE PRODUCT / TOOL (the buyer uses it themselves).\n"
        "- STRONG lead: a buyer who wants a TOOL/software/app to do it themselves, faster or cheaper.\n"
        "- WEAK / MISMATCH (score ≤0.4): a buyer who explicitly wants to HIRE a human studio/agency/"
        "freelancer (done-for-you) — wrong modality for a self-serve tool."
    ),
    "marketplace_platform": (
        "SELLER DELIVERY MODEL: MARKETPLACE / PLATFORM. STRONG lead: a buyer who would join or "
        "transact on such a platform for whichever side the seller monetises."
    ),
}


def _dossier_block(dossier: dict) -> str:
    """Compact, cache-friendly summary of the Seller Brain dossier — the SHARED context
    both live (stated-intent) and company (trigger) leads are scored against."""
    if not dossier:
        return ""
    segs = "; ".join(f"{s.get('name')} (fit {s.get('fit')})" for s in (dossier.get("core_segments") or [])[:6])
    sigs = "; ".join((dossier.get("need_signals") or [])[:6])
    excl = ", ".join(dossier.get("exclude") or [])
    return ("SELLER DOSSIER — score fit against THIS:\n"
            f"- Ranked target segments: {segs}\n"
            f"- Real need-signals (genuine intent): {sigs}\n"
            f"- Exclude (not a fit): {excl}")


async def score_signal(
    signal_text: str,
    signal_type: str,
    user_context: str,
    icp_text: str,
    delivery_model: str = None,
    dossier: dict = None,
) -> dict:
    """Score one signal. Uses prompt caching on ICP context."""

    modality = _MODALITY_RULE.get(delivery_model, "")

    # Hiring signals get their own scoring lens: a job post is a company publicly investing in
    # the seller's function NOW. On-modality only when the hire is a role that COMMISSIONS work.
    hiring_rule = ""
    if signal_type == "hiring":
        hiring_rule = """

THIS IS A HIRING SIGNAL (a company posting a job). Score it as stated-investment intent:
- STRONG (0.75+): the role OWNS or COMMISSIONS the exact function this seller powers (e.g. a
  Head of Content / VP Marketing / Brand lead for a video/content seller) AND the company plausibly
  sits in the seller's vertical. Building the function → they will need outside production help now.
- WEAK / MISMATCH (≤0.4): the role is a JUNIOR / execution hire meant to do the work IN-HOUSE
  (e.g. "Junior Video Editor", "in-house motion designer") — that REPLACES outsourcing, it's a
  negative signal for a service seller. Also weak: off-vertical company, a staffing/recruiting
  agency, or a COMPETITOR/vendor hiring to build a rival product.
Judge the ROLE's fit to the offering, not just the company. Quote the role/company as proof."""

    # Cached block — ICP + user_context + delivery model + dossier never change within a run
    # Anthropic caches this after first call, ~90% input token saving on repeats
    cached_context = f"""Seller profile (what they offer):
{user_context[:2000]}

Seller ICP (ideal customer):
{icp_text[:2000]}

{modality}
{_dossier_block(dossier)}"""

    user_message = f"""Signal to score:
Type: {signal_type}
Text: {signal_text[:1500]}

Score how likely this signal is a genuine, in-market buyer for THIS seller, respecting the
seller's delivery model above (modality must match — do not pass a tool-seeker to a studio or
a studio-seeker to a tool). A STATED need ("looking for / want to hire / need someone to make X")
is high quality. An INFERRED trigger (raised funding, launched a product) only implies a POSSIBLE
future need — score it ≤0.55 UNLESS one of these holds:
  (a) the trigger is explicitly about this exact service ("raised funding to scale the work this seller does"), OR
  (b) the company operates squarely in the seller's CORE vertical (per the dossier's top segments) such
      that growth DIRECTLY drives this need — a company in that exact vertical raising money, launching,
      or expanding WILL need more of what this seller provides → that's a STRONG trigger (0.7+), not weak.
A generic raise by a company OUTSIDE the seller's core vertical stays weak (≤0.5) — money alone doesn't
mean they need THIS service.
For company_name, extract the company OR the person's name/handle if it's an individual posting.{hiring_rule}"""

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


async def judge_leads(leads: list[dict], user_context: str, icp_text: str,
                      delivery_model: str = None, dossier: dict = None) -> dict:
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
need (a company in one of the dossier's top segments that raised, launched, expanded, or is hiring/
seeking what this seller provides) — these are exactly the target accounts, keep them
even if not perfectly worded.

SELLER (what they offer):
{user_context[:1800]}

SELLER'S IDEAL CUSTOMER:
{icp_text[:1800]}

{modality}
{_dossier_block(dossier)}

For EACH candidate decide:
- keep=true ONLY if it's a real, on-ICP, MODALITY-MATCHED buyer for THIS seller with a genuine
  reason-to-act-now — ideally a STATED need (they actually said they want this), or a trigger that
  is explicitly about this exact service.
- keep=false if the buyer wants the WRONG modality (wants a self-serve tool when the seller is a
  studio/agency, or wants to hire a studio when the seller is a self-serve tool).
- keep=false for INFERRED triggers with no stated need that are also OUTSIDE the core vertical:
  an off-vertical brand that just "raised money so they'll probably need this" is a guess, not intent.
  BUT keep a trigger if it explicitly ties to this service OR the company is squarely in the seller's
  CORE vertical where growth directly drives the need (a company in a dossier top-segment that raised,
  launched, or expanded WILL need more of what the seller provides → keep). Core-vertical raises are
  leads; off-vertical raises are not.
- keep=false if: COMPETITOR, WRONG vertical/industry, vanity milestone, or no real buying signal.
- competitor=true if it sells/builds the same offering as the seller (set keep=false too).

Candidates:
{listing}

Return ONLY a JSON array, one object per candidate IN ORDER:
[{{"i": 0, "keep": true, "competitor": false, "reason": "short"}}, ...]"""

    try:
        resp = await client.messages.create(
            model=SONNET_MODEL,
            max_tokens=4000,
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


async def generate_outreach(leads: list[dict], user_context: str, icp_text: str, dossier: dict = None) -> list[dict]:
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

The buyer's own words / phrasing (echo this voice, don't sound like a vendor): {", ".join((dossier or {}).get("buyer_language", [])[:8]) or "n/a"}

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


async def rank_by_dossier_fit(candidates: list[dict], dossier: dict, keep_threshold: float = 0.6) -> list[dict]:
    """CAVEAT SOLUTION (precision↔recall). When we cast a WIDE net (e.g. Exa agent returning
    ~1700 results, or broad funding), we don't filter with dumb vector/keyword gates — we rank
    every candidate by DEEP FIT to the seller dossier. One batched Sonnet pass scores each
    candidate 0-1 against the dossier's ranked segments + need-signals, tags the best-fit segment,
    and we keep those ≥ keep_threshold, sorted. Turns high recall into clean, ranked precision.

    candidates: [{"company_name","domain"/"company_domain","summary"/"description"}]
    Returns the kept candidates with added {"fit": float, "segment": str}, sorted by fit desc.
    """
    if not candidates:
        return []

    segs = "\n".join(f"  - [{s.get('fit')}] {s.get('name')}: {s.get('why','')[:90]}"
                     for s in dossier.get("core_segments", []))
    signals = "\n".join(f"  - {s}" for s in dossier.get("need_signals", []))
    items = []
    for i, c in enumerate(candidates):
        dom = c.get("domain") or c.get("company_domain") or ""
        desc = (c.get("summary") or c.get("description") or "")[:160]
        items.append(f"{i}. {c.get('company_name','?')} ({dom}) — {desc}")
    listing = "\n".join(items)

    prompt = f"""You are matching candidate companies against a seller's TARGETING DOSSIER. Score how
well each candidate fits as a CUSTOMER for this seller — be discerning, this is the quality gate that
turns a wide net into precision.

SELLER OFFERING: {dossier.get('offering','')}

RANKED TARGET SEGMENTS (higher fit = better):
{segs}

SIGNALS THAT MEAN A COMPANY NEEDS THIS SELLER:
{signals}

EXCLUDE (not a fit): {', '.join(dossier.get('exclude', []))}

For EACH candidate return its fit 0.0-1.0 (1.0 = textbook customer in a top segment; ≤0.3 = off-ICP
or excluded) and the best-matching segment name (or "none").

Candidates:
{listing}

Return ONLY a JSON array, one per candidate IN ORDER:
[{{"i": 0, "fit": 0.0-1.0, "segment": "..."}}, ...]"""

    try:
        resp = await client.messages.create(
            model=SONNET_MODEL, max_tokens=3000,
            messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        verdicts = json.loads(raw)
    except Exception as e:
        logger.warning(f"[DossierRank] failed, returning candidates unranked: {e}")
        return candidates

    vmap = {v.get("i"): v for v in verdicts if isinstance(v, dict)}
    kept = []
    for i, c in enumerate(candidates):
        v = vmap.get(i)
        if not v:
            continue
        fit = float(v.get("fit", 0))
        if fit >= keep_threshold:
            kept.append({**c, "fit": round(fit, 2), "segment": v.get("segment", "")})
    kept.sort(key=lambda x: x["fit"], reverse=True)
    logger.info(f"[DossierRank] {len(candidates)} candidates → {len(kept)} kept (fit ≥ {keep_threshold})")
    return kept


async def _call_llm(model: str, messages: list) -> dict:
    for attempt in range(3):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=256,
                system=SYSTEM_PROMPT,
                messages=messages,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1].lstrip("json").strip()
            result = json.loads(text)

            # Log cache performance
            usage = response.usage
            if hasattr(usage, "cache_read_input_tokens") and usage.cache_read_input_tokens:
                logger.debug(f"[Scoring] Cache hit — {usage.cache_read_input_tokens} tokens from cache")

            return result
        except json.JSONDecodeError:
            logger.warning(f"[Scoring] Bad JSON on attempt {attempt + 1}")
        except Exception as e:
            logger.error(f"[Scoring] LLM call failed: {e}")
            break
    return {"score": 0, "why": "scoring failed", "company_name": ""}
