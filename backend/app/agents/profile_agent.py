"""
PROFILE AGENT
=============
Runs ONCE per profile on creation (and again if user updates their ICP).
Takes ~30 seconds. Crawls user's website + LinkedIn and builds their ICP.

Flow:
  1. Crawl4AI deep-crawls website (homepage + case studies/work/about sub-pages)
     Falls back to Jina if Crawl4AI not installed
  2. Claude Sonnet reads all crawled content + user's text inputs
  3. Generates UserContext + 3 ICP options
  4. Returns options to frontend for user approval
  5. On approval: stores text + generates vector embedding

This is the FOUNDATION. Everything else depends on the ICP being good.
"""

import re
import logging
import httpx
from urllib.parse import urlparse
from app.llm import Anthropic
from app.database import supabase
from app.config import ANTHROPIC_API_KEY, SERPER_API_KEY
from app.models import ICPOption

SERPER_SEARCH_URL = "https://google.serper.dev/search"

logger = logging.getLogger(__name__)
client = Anthropic(api_key=ANTHROPIC_API_KEY)

JINA_BASE = "https://r.jina.ai/"

# Crawl4AI — best quality (JS, stealth). Falls back to Jina if not installed.
try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    from crawl4ai.content_filter_strategy import PruningContentFilter
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    HAS_CRAWL4AI = True
    logger.info("[ProfileAgent] crawl4ai available")
except ImportError:
    HAS_CRAWL4AI = False
    logger.info("[ProfileAgent] crawl4ai not installed — using Jina fallback")

# Sub-pages most likely to contain client/case study/service intel
PRIORITY_PATH_KEYWORDS = [
    "case-stud", "work", "portfolio", "clients", "about",
    "services", "projects", "results", "success", "showcase"
]


async def crawl_url_jina(url: str, char_limit: int = 12000) -> str:
    """Jina Reader fallback — no install required."""
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.get(f"{JINA_BASE}{url}", headers={"Accept": "text/plain"})
            return resp.text[:char_limit] if resp.status_code == 200 else ""
    except Exception as e:
        logger.warning(f"[ProfileAgent] Jina crawl failed for {url}: {e}")
        return ""


async def crawl_url_crawl4ai(url: str, char_limit: int = 12000) -> str:
    """Crawl4AI — JS rendering + stealth. Returns clean markdown."""
    try:
        browser_cfg = BrowserConfig(headless=True, verbose=False)
        md_gen = DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(threshold=0.45, threshold_type="fixed")
        )
        run_cfg = CrawlerRunConfig(markdown_generator=md_gen)
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await crawler.arun(url=url, config=run_cfg)
            if result.success and result.markdown:
                text = result.markdown.fit_markdown or result.markdown.raw_markdown
                return text[:char_limit]
    except Exception as e:
        logger.warning(f"[ProfileAgent] Crawl4AI failed for {url}: {e}")
    return ""


async def crawl_url(url: str, char_limit: int = 12000) -> str:
    """Crawl a URL — uses Crawl4AI if available, else Jina."""
    if HAS_CRAWL4AI:
        text = await crawl_url_crawl4ai(url, char_limit)
        if text:
            return text
    return await crawl_url_jina(url, char_limit)


def extract_internal_links(text: str, base_url: str) -> list[str]:
    """Pull internal links from Jina-rendered text and return priority sub-pages."""
    base = urlparse(base_url)
    base_domain = f"{base.scheme}://{base.netloc}"

    # Jina renders links as [text](url) — grab all markdown links
    found = re.findall(r'\[.*?\]\((https?://[^\)]+)\)', text)
    # Also grab bare URLs
    found += re.findall(r'https?://[^\s\)\"\']+', text)

    seen = set()
    priority = []
    for raw in found:
        url = raw.strip().rstrip(".,)")
        parsed = urlparse(url)
        if parsed.netloc != base.netloc:
            continue
        path = parsed.path.lower()
        if any(kw in path for kw in PRIORITY_PATH_KEYWORDS):
            clean = f"{base_domain}{parsed.path}"
            if clean not in seen and clean != base_url:
                seen.add(clean)
                priority.append(clean)

    return priority[:4]  # max 4 sub-pages


async def deep_crawl_website(website_url: str) -> str:
    """
    Crawl homepage + up to 3 priority sub-pages.
    Returns combined text labelled by page type.
    """
    if not website_url:
        return ""

    homepage_text = await crawl_url(website_url, char_limit=10000)
    if not homepage_text:
        return ""

    sections = [f"[HOMEPAGE]\n{homepage_text}"]

    sub_urls = extract_internal_links(homepage_text, website_url)
    logger.info(f"[ProfileAgent] Found {len(sub_urls)} priority sub-pages: {sub_urls}")

    for sub_url in sub_urls[:3]:
        text = await crawl_url(sub_url, char_limit=6000)
        if text and len(text) > 300:
            page_type = next((kw.upper() for kw in PRIORITY_PATH_KEYWORDS if kw in sub_url.lower()), "PAGE")
            sections.append(f"[{page_type}: {sub_url}]\n{text}")
            logger.info(f"[ProfileAgent] Crawled sub-page: {sub_url} ({len(text)} chars)")

    return "\n\n---\n\n".join(sections)


SYSTEM_PROMPT = """You are a B2B sales intelligence expert specialising in agency outbound and intent-based targeting.

Your job is not to describe who a company is — it is to identify the exact moment they become ready to buy.
A good ICP is a trigger, not a demographic.

Rules you must follow:
1. ALWAYS trust the user's own words (what they sell, who they target) over crawled content. If they conflict, use the user's words and note the discrepancy.
2. NEVER produce generic ICPs. "B2B SaaS, 50-500 employees, Head of Marketing" is a failure. Specificity of moment is everything.
3. Trigger events must be OBSERVABLE and SEARCHABLE — something an agent can find on Reddit, a job board, a news site, or LinkedIn without calling the company.
4. The Signal-Based ICP must be defined entirely by behaviour and events — no firmographics, no industry filters. Pure intent signals only.
5. If the crawled data is thin or missing, say so clearly in the user_context and make your sharpest specific guess from what you have.
6. Extract these signals from the website if present: existing client types, case study industries, pricing tier signals (enterprise vs SMB language), repeated pain points in their copy.
7. Extract these signals from LinkedIn if present: founder's background and previous roles, team composition (hints at service capacity), types of companies that engage with their content.

8. Website content may include multiple pages labelled [HOMEPAGE], [CASE-STUD: ...], [WORK: ...] etc. Mine each page:
   - Homepage: positioning, what problem they solve
   - Case studies / work pages: actual client names, industries, outcomes — this is the most valuable signal
   - About page: team size hints, founder background
   - Services page: exact service names, pricing tier language

Output quality bar: your ICPs should read like they were written by someone who has done 10 years of B2B outbound, not a marketing textbook."""


async def generate_icp_options(
    website_text: str,
    linkedin_text: str,
    service_description: str,
    target_description: str,
    research_evidence: str = "",
    system_prompt: str = None,
) -> tuple[str, list[ICPOption], dict]:
    """
    Call Claude to produce:
      - UserContext.md (what this agency sells, tone, differentiators)
      - 3 ICP options (broad / niche / signal-based)

    Returns: (user_context_text, [ICPOption, ICPOption, ICPOption])
    """
    import json

    # Flag data quality so Claude knows what to trust
    website_quality = "EMPTY — rely on user inputs only" if not website_text or len(website_text) < 200 else "OK"
    linkedin_quality = "EMPTY OR BLOCKED — do not factor in" if not linkedin_text or len(linkedin_text) < 200 else "OK"

    user_message = f"""Here is everything I know about this agency. Build their ICP.

--- USER'S OWN WORDS (highest trust) ---
What they sell: {service_description}
Who they target: {target_description}

--- WEBSITE CONTENT [quality: {website_quality}] ---
{website_text or "Not available"}

--- LINKEDIN CONTENT [quality: {linkedin_quality}] ---
{linkedin_text or "Not available"}

--- EXTERNAL RESEARCH EVIDENCE (independent of their own marketing — weight this heavily; it reflects who ACTUALLY buys) ---
{research_evidence or "Not available"}

---

Produce two things:

1. UserContext — a sharp internal profile of this agency:
   - What they actually sell (cut through any marketing fluff)
   - Pricing tier signal (enterprise, mid-market, SMB — infer from language and clients)
   - Their tone and positioning
   - Key differentiators (what they keep repeating)
   - Types of clients they've already worked with (from case studies, logos, testimonials)
   - Data quality note (flag anything missing or thin)

2. Three ICP options. Each must be meaningfully different:
   Option A — Broad ICP: wider net, more leads, lower precision. Still specific — not just an industry label.
   Option B — Niche ICP: the single tightest-fit customer type. Fewer leads, very high conversion potential.
   Option C — Signal-Based ICP: defined ONLY by observable trigger events and behaviours. No industry or size filters. Pure intent.

For each ICP include:
   - Target industry (skip for Signal-Based)
   - Company size range (skip for Signal-Based)
   - Exact buyer title (not "marketing person" — be specific)
   - The specific pain they're feeling RIGHT NOW that makes them ready to buy
   - Top 3 trigger events — each must be a real, searchable, observable moment (job posting, funding news, Reddit post, LinkedIn announcement, review site complaint, etc.)
   - One sharp one-line summary — describe the moment, not the company type

Respond in this exact JSON format with no markdown:
{{
  "user_context": "...",
  "icp_options": [
    {{
      "label": "Broad ICP",
      "summary": "...",
      "icp_text": "..."
    }},
    {{
      "label": "Niche ICP",
      "summary": "...",
      "icp_text": "..."
    }},
    {{
      "label": "Signal-Based ICP",
      "summary": "...",
      "icp_text": "..."
    }}
  ]
}}"""

    # Sonnet occasionally emits invalid JSON in the long prose fields (unescaped
    # quotes/commas). Retry a few times — it's stochastic, regeneration fixes it.
    last_err = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=4000,
                system=system_prompt or SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": "{"},  # prefill: force pure JSON, no preamble
                ],
            )

            raw = "{" + response.content[0].text  # re-add prefilled brace
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            data = _loads_tolerant(raw)
            user_context = data.get("user_context", "")
            icp_options = [ICPOption(**opt) for opt in data.get("icp_options", [])]
            if not icp_options:
                raise ValueError("no icp_options in response")

            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost_usd = (input_tokens / 1_000_000 * 3.00) + (output_tokens / 1_000_000 * 15.00)
            logger.info(f"[ProfileAgent] tokens in={input_tokens} out={output_tokens} cost=${cost_usd:.5f} (attempt {attempt+1})")
            return user_context, icp_options, {"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": round(cost_usd, 5)}

        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            logger.warning(f"[ProfileAgent] JSON parse failed (attempt {attempt+1}/3): {e}")
            continue
        except Exception as e:
            logger.error(f"[ProfileAgent] generate_icp_options failed: {e}")
            raise

    logger.error(f"[ProfileAgent] All 3 attempts returned malformed JSON: {last_err}")
    raise ValueError("ICP generation failed — please try again.")



def _loads_tolerant(raw: str) -> dict:
    """Parse JSON, with a light repair pass for common LLM mistakes."""
    import json as _json
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        # trim anything after the last closing brace, fix trailing commas
        end = raw.rfind("}")
        if end != -1:
            candidate = raw[: end + 1]
            import re as _re
            candidate = _re.sub(r",\s*([}\]])", r"\1", candidate)  # trailing commas
            return _json.loads(candidate)
        raise


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


SHARPEN_PROMPT = """You are sharpening a seller's ICP. The CURRENT ICP was auto-derived from their
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
4. GEOGRAPHY — do NOT fence the ICP to the clients' home country. The core vertical exists GLOBALLY;
   if the real clients cluster in one region (e.g. India), name that as a proven STRENGTH / beachhead,
   but define the TARGET MARKET as the vertical WORLDWIDE (North America, Europe, SEA, LatAm, India,
   etc.). A great-fit company abroad (e.g. a US/Ukraine/SEA microdrama or AI-media studio) is just as
   on-ICP as a domestic one. State geography as "global, with [region] as a current strength."
5. Keep it specific and outbound-usable. No fluff. ~180-260 words.

Return ONLY the rewritten ICP text (no preamble, no markdown headers)."""


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


DOSSIER_PROMPT = """You are an embedded analyst who knows this SELLER's business like an INSIDER —
their offering, their best-fit customers, and the exact moments a company needs them. Build a precise
TARGETING DOSSIER that will be used to query the web (especially Exa neural search) with surgical
precision — so we find companies that are ALREADY a fit, instead of searching broad and filtering.

SELLER CONTEXT (website + positioning):
{user_context}

SELLER'S OWN ANSWERS (HIGHEST TRUST — they know their business):
{intake}

RESEARCH on their real clients + dream-fit companies (what these companies ACTUALLY are):
{research}

Build the dossier with these rules:
- If the seller listed KEYWORDS in their answers, treat them as the EXACT vocabulary of their vertical — use them verbatim in core_segments, need_signals, and exa_queries.
- core_segments: RANK the buyer segments best-fit first. Be specific to the seller's ACTUAL vertical — name the precise customer category (inferred from their clients, dream companies, and keywords), not a broad label like "media" or "SaaS". fit = 1-10.
- anchor_companies: 6-12 REAL, specific companies that are the PUREST examples of a great customer — a mix of their actual clients and dream-fit names. These seed Exa find_similar, so they must be real and on-target.
- need_signals: the OBSERVABLE, searchable signals that mean "this company needs the seller NOW" (funding for the exact use case, hiring a relevant role, launching a slate, expanding format/market). Not vague.
- buyer_language: the actual words/phrases a buyer at these companies would use when they have the need.
- exa_queries: 4-6 RICH, natural-language semantic queries an insider would run on Exa to find these companies — descriptive sentences, NOT keyword soup. Each names the precise customer type (in the SELLER's own vertical + their keywords) + their situation + a recent trigger. Match the seller's vertical, not any example vertical.

Return ONLY valid JSON, no markdown:
{{
  "offering": "one sharp line — what they sell + how they deliver",
  "delivery_model": "service_or_agency | self_serve_product | marketplace_platform",
  "core_segments": [{{"name": "...", "why": "...", "fit": 9}}],
  "anchor_companies": ["...", "..."],
  "need_signals": ["...", "..."],
  "buyer_titles": ["...", "..."],
  "buyer_language": ["...", "..."],
  "geo": "global, with [region] as strength — or specific",
  "exclude": ["competitors / off-fit to avoid"],
  "exa_queries": ["rich natural-language query 1", "..."]
}}"""


# ---------------------------------------------------------------------------
# REFINE — conversational dossier editor (the "Ask cnvrted" chatbot)
# ---------------------------------------------------------------------------

_REFINE_TOOL = {
    "name": "emit_dossier_patch",
    "description": "Apply the seller's plain-English feedback to their targeting dossier.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "1-2 friendly sentences to the seller, summarizing what changed"},
            "add_exclude": {"type": "array", "items": {"type": "string"}, "description": "company names OR types to exclude"},
            "remove_segments": {"type": "array", "items": {"type": "string"}, "description": "EXACT names of core_segments to drop"},
            "add_segments": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "why": {"type": "string"}, "fit": {"type": "integer"}}}},
            "reweight": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "fit": {"type": "integer"}}}, "description": "adjust fit (1-10) of existing segments by exact name"},
            "set_geo": {"type": "string"},
            "add_anchors": {"type": "array", "items": {"type": "string"}},
            "add_need_signals": {"type": "array", "items": {"type": "string"}},
            "set_buyer_titles": {"type": "array", "items": {"type": "string"}},
            "exa_queries": {"type": "array", "items": {"type": "string"},
                            "description": "ONLY if a structural change makes the old queries stale — return the FULL fresh set (4-6); else omit"},
        },
        "required": ["reply"],
    },
}

_REFINE_PROMPT = """The seller is refining their targeting dossier with feedback. Read their message + the
current dossier, then emit a PATCH reflecting their intent. Be precise and CONSERVATIVE — change only what
they asked; NEVER wipe the dossier. Keep their vertical intact unless they explicitly change it.

CURRENT DOSSIER:
{dossier}

SELLER FEEDBACK:
{message}

Guidance:
- If the message is a greeting, a question, off-topic, or too vague to act on, make NO changes — return ONLY a friendly reply asking what they'd like to adjust (give 1-2 examples). Leave every change field empty.
- "these X aren't my buyers" → add X to add_exclude AND remove/down-weight the matching segment.
- "focus more on X / less on Y" → reweight (raise X, lower Y) or add_segments.
- geo change → set_geo, and if it changes WHO you'd target, return a fresh full exa_queries set.
- If segments/anchors/geo changed enough that the search queries are now stale, return a FRESH FULL
  exa_queries set (4-6, in the seller's OWN vertical); otherwise omit exa_queries.
- reply: talk to the seller in 1-2 warm sentences about exactly what you changed."""


async def refine_dossier(profile_id: str, message: str) -> dict:
    """Apply NL feedback to the dossier. Returns {reply, rebuilding, removed}.
    Fully guarded — on any failure the dossier is untouched."""
    import json
    if not (message or "").strip():
        return {"reply": "Tell me what to change about your list.", "rebuilding": False, "removed": 0}
    p = supabase.table("user_profiles").select("search_profile").eq("id", profile_id).execute()
    sp = (p.data[0].get("search_profile") if p.data else None) or {}
    dossier = sp.get("dossier")
    if not dossier:
        return {"reply": "Build a profile first (ICP Config), then I can refine your list.", "rebuilding": False, "removed": 0}
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=2000,
            tools=[_REFINE_TOOL], tool_choice={"type": "tool", "name": "emit_dossier_patch"},
            messages=[{"role": "user", "content": _REFINE_PROMPT.format(
                dossier=json.dumps(dossier)[:4000], message=message[:600])}])
        patch = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    except Exception as e:
        logger.error(f"[refine] LLM failed: {e}")
        return {"reply": "Sorry, I hit an error — try rephrasing.", "rebuilding": False, "removed": 0}
    if not patch:
        return {"reply": "I couldn't parse that — try rephrasing.", "rebuilding": False, "removed": 0}

    # No-op guard: greeting / question / vague input → reply only, never touch the dossier.
    _change_keys = ("add_exclude", "remove_segments", "add_segments", "reweight",
                    "set_geo", "add_anchors", "add_need_signals", "set_buyer_titles", "exa_queries")
    if not any(patch.get(k) for k in _change_keys):
        return {"reply": patch.get("reply") or "Tell me what to change — e.g. “exclude big enterprises” or “focus more on X.”",
                "rebuilding": False, "removed": 0}

    prev = json.loads(json.dumps(dossier))   # deep copy for undo
    structural = False
    if patch.get("add_exclude"):
        dossier["exclude"] = list(dict.fromkeys((dossier.get("exclude") or []) + patch["add_exclude"]))
    if patch.get("remove_segments"):
        drop = {n.lower() for n in patch["remove_segments"]}
        dossier["core_segments"] = [s for s in dossier.get("core_segments", []) if (s.get("name") or "").lower() not in drop]
        structural = True
    if patch.get("add_segments"):
        dossier["core_segments"] = dossier.get("core_segments", []) + patch["add_segments"]; structural = True
    if patch.get("reweight"):
        wmap = {w["name"].lower(): w.get("fit") for w in patch["reweight"] if w.get("name")}
        for s in dossier.get("core_segments", []):
            if (s.get("name") or "").lower() in wmap:
                s["fit"] = wmap[(s["name"] or "").lower()]
    if patch.get("set_geo"):
        dossier["geo"] = patch["set_geo"]; structural = True
    if patch.get("add_anchors"):
        dossier["anchor_companies"] = list(dict.fromkeys((dossier.get("anchor_companies") or []) + patch["add_anchors"])); structural = True
    if patch.get("add_need_signals"):
        dossier["need_signals"] = list(dict.fromkeys((dossier.get("need_signals") or []) + patch["add_need_signals"]))
    if patch.get("set_buyer_titles"):
        dossier["buyer_titles"] = patch["set_buyer_titles"]
    if patch.get("exa_queries"):
        dossier["exa_queries"] = patch["exa_queries"]; structural = True
    dossier["core_segments"] = sorted(dossier.get("core_segments", []), key=lambda s: s.get("fit", 0), reverse=True)

    sp["dossier"] = dossier; sp["dossier_prev"] = prev
    supabase.table("user_profiles").update({"search_profile": sp}).eq("id", profile_id).execute()

    removed = _apply_excludes(profile_id, patch.get("add_exclude") or [])
    return {"reply": patch.get("reply", "Updated your targeting."), "rebuilding": structural, "removed": removed}


def _apply_excludes(profile_id: str, excludes: list) -> int:
    """Instantly drop excluded companies from the Target List + cached leads (explicit names)."""
    ex = [e.lower() for e in (excludes or []) if e and len(e) >= 4]
    if not ex:
        return 0
    removed = 0
    try:
        rows = supabase.table("watchlist_companies").select("id, company_name") \
            .eq("profile_id", profile_id).execute().data or []
        for r in rows:
            n = (r.get("company_name") or "").lower()
            if n and any(e in n or n in e for e in ex):
                supabase.table("watchlist_companies").delete().eq("id", r["id"]).execute()
                removed += 1
    except Exception as e:
        logger.warning(f"[refine] watchlist exclude failed: {e}")
    try:
        from app.routes.leads_v2 import remove_companies_from_cache
        remove_companies_from_cache(profile_id, excludes)
    except Exception as e:
        logger.warning(f"[refine] leads exclude failed: {e}")
    return removed


async def undo_refine(profile_id: str) -> dict:
    """Revert the last refine (swap dossier_prev back in)."""
    p = supabase.table("user_profiles").select("search_profile").eq("id", profile_id).execute()
    sp = (p.data[0].get("search_profile") if p.data else None) or {}
    prev = sp.get("dossier_prev")
    if not prev:
        return {"reply": "Nothing to undo.", "rebuilding": False}
    sp["dossier"] = prev
    sp.pop("dossier_prev", None)
    supabase.table("user_profiles").update({"search_profile": sp}).eq("id", profile_id).execute()
    return {"reply": "Reverted the last change.", "rebuilding": True}


async def rebuild_precision(profile_id: str):
    """Background: rebuild the precision Target List from the (updated) dossier."""
    try:
        p = supabase.table("user_profiles").select("search_profile").eq("id", profile_id).execute()
        dossier = ((p.data[0].get("search_profile") if p.data else None) or {}).get("dossier")
        if dossier:
            from app.agents.watchlist_agent import build_precision_targets
            await build_precision_targets(profile_id, dossier)
    except Exception as e:
        logger.error(f"[refine] precision rebuild failed: {e}")


async def build_seller_dossier(profile_id: str, intake: dict = None) -> dict:
    """The 'Seller Brain' — fuse website + client/dream-company research + the seller's intake
    answers into a precise targeting DOSSIER (ranked segments, anchor companies, need-signals,
    buyer language, ready-to-run Exa semantic queries). Foundation for precision querying.
    Returns the dossier dict. Does NOT write to DB (caller decides)."""
    import json
    p = supabase.table("user_profiles").select("user_context, icp_text, search_profile, website_url") \
        .eq("id", profile_id).execute()
    if not p.data:
        return {"error": "profile not found"}
    row = p.data[0]
    user_context = row.get("user_context", "") or row.get("icp_text", "") or ""
    # Fresh-crawl the site so the dossier reflects the CURRENT site (not a stale onboarding crawl)
    try:
        if row.get("website_url"):
            site = await deep_crawl_website(row["website_url"])
            if site:
                user_context = f"{site[:3000]}\n\n{user_context}"
    except Exception as e:
        logger.warning(f"[Dossier] fresh crawl failed, using stored context: {e}")
    sp = row.get("search_profile") or {}
    intake = intake or {}

    # Anchor candidates to research = the seller's clients + any dream-fit companies they named
    anchors = list(dict.fromkeys(
        (sp.get("lookalike_companies") or []) + (intake.get("dream_companies") or [])))
    research = await research_clients(anchors) if anchors else "(no client/dream companies provided)"
    logger.info(f"[Dossier] researched {len(anchors)} anchor companies")

    intake_text = json.dumps(intake, indent=2) if intake else "(no structured answers provided yet)"
    prompt = DOSSIER_PROMPT.format(
        user_context=user_context[:1800], intake=intake_text[:1500], research=research[:2000])

    # TOOL-USE for guaranteed-valid structured output (no JSON string parsing — Opus's
    # long-prose JSON kept breaking the parser; tool_choice returns a clean dict every time).
    # OPUS here: the dossier is the foundation everything reads + built once per profile (cached),
    # so the best model is worth it; hot-path scoring stays Haiku.
    tool = {
        "name": "emit_dossier",
        "description": "Return the seller targeting dossier.",
        "input_schema": {
            "type": "object",
            "properties": {
                "offering": {"type": "string"},
                "delivery_model": {"type": "string"},
                "core_segments": {"type": "array", "items": {"type": "object", "properties": {
                    "name": {"type": "string"}, "why": {"type": "string"}, "fit": {"type": "integer"}}}},
                "anchor_companies": {"type": "array", "items": {"type": "string"}},
                "need_signals": {"type": "array", "items": {"type": "string"}},
                "buyer_titles": {"type": "array", "items": {"type": "string"}},
                "buyer_language": {"type": "array", "items": {"type": "string"}},
                "geo": {"type": "string"},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "exa_queries": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["offering", "delivery_model", "core_segments", "anchor_companies",
                         "need_signals", "exa_queries"],
        },
    }
    try:
        resp = client.messages.create(
            model="claude-opus-4-8", max_tokens=2500,
            tools=[tool], tool_choice={"type": "tool", "name": "emit_dossier"},
            messages=[{"role": "user", "content": prompt}])
        dossier = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        if not dossier:
            return {"error": "no tool_use block in dossier response"}
        logger.info(f"[Dossier] built: {len(dossier.get('core_segments',[]))} segments, "
                    f"{len(dossier.get('anchor_companies',[]))} anchors, {len(dossier.get('exa_queries',[]))} exa queries")
        return dossier
    except Exception as e:
        logger.error(f"[Dossier] failed: {e}")
        return {"error": str(e)}


async def _dream_fit_reasons(dreams: list, dossier: dict) -> dict:
    """One Haiku call → a concrete, dossier-grounded fit reason per dream company.
    Never the label 'dream account' — the ACTUAL reason it fits the ICP. {} on failure."""
    import json as _json
    if not dreams:
        return {}
    segs = "; ".join((s.get("name") or "") for s in (dossier.get("core_segments") or [])[:6])
    prompt = (f"Seller offering: {(dossier.get('offering') or '')[:200]}\n"
              f"Target segments: {segs}\n\n"
              f"For EACH company, write ONE concrete reason (<14 words) why it's a strong-fit "
              f"CUSTOMER for this seller — its business model / why it needs this. "
              f"NOT 'dream account', NOT generic praise.\n"
              f"Companies: {', '.join(dreams)}\n\n"
              f'Return ONLY JSON: {{"Company Name": "reason", ...}}')
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=400,
            messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        out = _json.loads(raw)
        return out if isinstance(out, dict) else {}
    except Exception as e:
        logger.warning(f"[SellerBrain] dream fit-reasons failed: {e}")
        return {}


async def build_seller_brain(profile_id: str, intake: dict = None) -> dict:
    """One entrypoint to set up a profile for PRECISION targeting:
      1. build the seller DOSSIER (deep understanding)
      2. PERSIST it on the profile (search_profile.dossier — no migration)
      3. build the precision Target List from the dossier's Exa queries
      4. add the founder's named DREAM companies as targets (their explicit input)
    Returns a summary. Idempotent-ish (upserts)."""
    intake = intake or {}
    dossier = await build_seller_dossier(profile_id, intake)
    if dossier.get("error"):
        return {"error": dossier["error"]}

    # 2. persist dossier inside search_profile (merge, don't clobber existing facets)
    row = supabase.table("user_profiles").select("search_profile").eq("id", profile_id).execute().data
    sp = (row[0].get("search_profile") if row else None) or {}
    sp["dossier"] = dossier
    supabase.table("user_profiles").update({"search_profile": sp}).eq("id", profile_id).execute()

    # 3. precision targets from the dossier's insider Exa queries
    from app.agents.watchlist_agent import build_precision_targets
    targets = await build_precision_targets(profile_id, dossier)

    # 4. add the founder's DREAM companies as monitored targets (their explicit input,
    #    not seeding — these are the accounts they'd kill to land). Never label them
    #    "dream account" in the UI — show the ACTUAL reason each one fits the ICP.
    dreams = [(d or "").strip() for d in (intake.get("dream_companies") or []) if (d or "").strip()]
    reasons = await _dream_fit_reasons(dreams, dossier)
    _fallback = (dossier.get("core_segments") or [{}])[0].get("name") or "on-ICP target"
    dreams_added = 0
    for n in dreams:
        try:
            supabase.table("watchlist_companies").upsert({
                "profile_id": profile_id, "company_name": n,
                "reason": reasons.get(n) or _fallback, "source": "dream_target",
            }, on_conflict="profile_id,company_name").execute()
            dreams_added += 1
        except Exception as e:
            logger.debug(f"[SellerBrain] dream upsert failed for {n}: {e}")

    logger.info(f"[SellerBrain] dossier stored, {len(targets)} precision targets, {dreams_added} dream targets")
    return {
        "dossier": dossier,
        "precision_targets": len(targets),
        "dream_targets": dreams_added,
        "segments": [s.get("name") for s in dossier.get("core_segments", [])],
    }


async def generate_icp_vector(icp_text: str) -> list[float]:
    """
    Convert ICP text to a 1536-dim vector using OpenAI text-embedding-3-small.
    Delegates to matching.vectorise_text so both ICP and signals use identical model.
    """
    from app.pipeline.matching import vectorise_text
    return await vectorise_text(icp_text)


async def run(profile_id: str) -> dict:
    """
    Entry point. Called once when a profile is created.
    Returns the 3 ICP options for user to choose from.
    """
    logger.info(f"[ProfileAgent] Running for profile_id={profile_id}")

    # 1. Load profile from DB
    result = supabase.table("user_profiles").select("*").eq("id", profile_id).execute()
    if not result.data:
        return {"error": "Profile not found"}

    profile = result.data[0]

    # 2. Deep crawl website (homepage + case study/portfolio sub-pages) + LinkedIn
    website_text = await deep_crawl_website(profile.get("website_url", ""))
    linkedin_text = await crawl_url(profile.get("linkedin_url", ""), char_limit=6000)
    logger.info(f"[ProfileAgent] website={len(website_text)} chars, linkedin={len(linkedin_text)} chars")

    # 3. Generate ICP options
    user_context, icp_options, _ = await generate_icp_options(
        website_text,
        linkedin_text,
        profile.get("service_description", ""),
        profile.get("target_description", ""),
    )

    # 4. Store user_context in DB (ICP not stored yet — waiting for user approval)
    supabase.table("user_profiles").update({
        "user_context": user_context
    }).eq("id", profile_id).execute()

    return {
        "user_context": user_context,
        "icp_options": [opt.dict() for opt in icp_options]
    }


async def approve_icp(profile_id: str, icp_text: str) -> dict:
    """
    Called when user approves an ICP option (or submits custom ICP).
    Stores the ICP text and generates the vector embedding.
    """
    logger.info(f"[ProfileAgent] Approving ICP for profile_id={profile_id}")

    # 1. Generate vector
    icp_vector = await generate_icp_vector(icp_text)

    # 2. Build search facets from ICP + stored user_context
    result = supabase.table("user_profiles").select("user_context").eq("id", profile_id).execute()
    user_context = (result.data[0].get("user_context") or "") if result.data else ""
    search_profile = await build_search_profile(icp_text, user_context)

    # 3. Store in DB
    supabase.table("user_profiles").update({
        "icp_text": icp_text,
        "icp_vector": icp_vector,
        "search_profile": search_profile,
    }).eq("id", profile_id).execute()

    # 4. SHARPEN — research the named clients/lookalikes and re-derive an evidence-weighted
    # ICP (core sweet-spot first). The homepage-derived ICP is broad; this grounds it in who
    # the seller ACTUALLY serves. Re-stores icp_text + icp_vector + search_profile.
    # Best-effort: if it fails, the (already-stored) homepage ICP stands.
    try:
        await sharpen_icp_from_clients(profile_id)
        logger.info(f"[ProfileAgent] ICP sharpened from client research for {profile_id}")
    except Exception as e:
        logger.warning(f"[ProfileAgent] client-research sharpen skipped: {e}")

    return {"status": "approved", "profile_id": profile_id}
