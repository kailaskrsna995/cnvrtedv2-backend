"""
SIGNAL ENGINE — dossier-driven weak-signal detectors (the intent moat, v1)
==========================================================================
THE IDEA (raindrops, not thunder). Strong, stated intent is a tiny pond. The
abundant form of intent is a company throwing off several WEAK signals at once —
each one noise on its own, but two or three on the SAME company = a real buyer.

The pipeline already rewards that: leads_v2 does company-level dedup and adds a
+0.10 stacking boost when a company shows up across >=2 distinct signal_types.
This engine's whole job is to FEED that stacking machine more, weaker, seller-
specific signal types — so a company lit up on funding AND a fresh content-leader
hire AND a slate launch floats up as a corroborated lead no single query produces.

DOSSIER-DRIVEN (generalizes to any seller). We do NOT hardcode "podcast launch".
The dossier already carries `need_signals` — the seller's own list of observable
events that mean a company needs them. A Haiku step turns each need_signal into a
DETECTOR RECIPE {label, query, what_counts, weight}; a generic Serper runner runs
each; a verify step confirms the article really evidences the need (precision over
the generic news agent); each recipe pushes signals under its OWN signal_type.

TWO DESIGN SEAMS (why this stays clean):
  1. STACKING needs distinct signal_types  → signal_type = recipe.label (varies).
  2. VECTOR GATE must be SKIPPED for these  → these are short, weakly-embedding
     texts that the ICP-vector gate would wrongly drop. We can't list dynamic
     labels in the gate's skip tuple, so we tag source_platform="signal_engine"
     and leads_v2 skips the gate on that flag (exactly how precision_exa does it).
     The dossier-aware scorer + Sonnet judge are the real filter, not cosine sim.

STRENGTH is the SCORER's job, not the recipe's. score_signal already caps an
inferred trigger at <=0.55 unless the company sits in a dossier core segment — so
weak detector signals naturally score low and only cross threshold via stacking.
recipe.weight is carried as metadata for now (future: soft ceilings / v2 tally),
never double-counted against the scorer.

v1 = per-scan (this file). v2 = persist each company's signals to a per-company
score-sheet and run detectors on a schedule (scheduler currently disabled) so the
tally compounds across days — the real moat.
"""

import re
import json
import hashlib
import logging
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from app.llm import Anthropic
from app import usage
from app.config import SERPER_API_KEY, ANTHROPIC_API_KEY
from app.queue import signal_queue

logger = logging.getLogger(__name__)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

SERPER_NEWS_URL = "https://google.serper.dev/news"

# Bounds — keep the added scoring cost predictable. Each surfaced signal becomes a
# Haiku scoring call downstream, so cap recipes and results-per-recipe.
MAX_RECIPES = 6
SERPER_NUM = 10
MAX_PER_RECIPE = 6          # verified signals kept per detector
MAX_TOTAL_SIGNALS = 30      # hard ceiling across all detectors in one run

# Aggregator/directory pages are profile pages, not trigger events — waste of a
# scoring call. (leads_v2 pre-filter also drops these; we skip them early to save cost.)
_JUNK_DOMAINS = (
    "tracxn.com", "crunchbase.com", "pitchbook.com", "zaubacorp.com",
    "linkedin.com/company", "glassdoor.", "owler.com", "cbinsights.com",
    "dnb.com", "rocketreach.co", "apollo.io", "zoominfo.com", "wikipedia.org",
)


# ---------------------------------------------------------------------------
# 1. Dossier need_signals -> detector recipes  (the generalization step)
# ---------------------------------------------------------------------------

_RECIPE_PROMPT = """A seller has the offering and target below. When a company is about to need this
seller, it throws off OBSERVABLE events reported in the news. Turn the seller's need-signals into
concrete, DISTINCT news detectors.

IMPORTANT — the seller ALREADY has dedicated agents watching plain FUNDING rounds and JOB POSTINGS.
Do NOT create detectors for "raised a round" or "is hiring for role X" — those lanes are covered, and
duplicating them just pulls generic funding/jobs noise. Focus ONLY on events that have NO dedicated
watcher, e.g.: a content SLATE / episode-count / title-target announcement; a new CHANNEL or FORMAT
launch (YouTube / vertical-video / microdrama / podcast-to-video); expansion from audio or text INTO
video; a public statement about adopting AI to cut production cost or lift output; a PARTNERSHIP with
a generative-AI tool; geographic expansion requiring localized video at scale.

SELLER OFFERING: {offering}

RANKED TARGET SEGMENTS (highest-fit first — weight detectors toward the top ones):
{segments}

THE SELLER'S OWN NEED-SIGNALS (observable events that mean a company needs this seller):
{need_signals}

GEO: {geo}

Produce {n} detectors. Each detector is a JSON object:
  "label":       a short snake_case signal name, 2-4 words (e.g. "content_slate_announced",
                 "microdrama_channel_launch", "audio_to_video_expansion"). This becomes the signal
                 type — make each label a DISTINCT event type so detectors can stack on a company.
  "query":       a Google News query that surfaces companies IN THE TOP SEGMENTS hitting this event.
                 Rules: under 9 words; end with the single year 2026 (no other year); use the
                 segments' concrete product-category words (the seller's real vertical), NOT abstract
                 jargon; NO company names; NO site: operator; NO quotation marks.
  "what_counts": one plain sentence — what an article must actually show to count as this signal.
  "weight":      a number 0.2-0.6 — how strong this event is as a buying signal on its own
                 (a concrete slate/format launch ~0.5; a vague activity mention ~0.25).

Make the detectors cover DIFFERENT event types from each other (not six flavors of the same launch).
Return ONLY a JSON array of {n} detector objects, no markdown."""


async def dossier_recipes(dossier: dict) -> list[dict]:
    """Map dossier.need_signals + ranked core_segments -> detector recipes.
    Returns [] on any failure so the caller can no-op cleanly."""
    if not dossier or not (dossier.get("need_signals") or dossier.get("core_segments")):
        return []
    try:
        segs = sorted(dossier.get("core_segments", []), key=lambda s: s.get("fit", 0), reverse=True)
        seg_lines = "\n".join(f"  - [{s.get('fit')}] {s.get('name','')}" for s in segs[:5]) or "  (none)"
        need_lines = "\n".join(f"  - {s}" for s in (dossier.get("need_signals") or [])[:8]) or "  (none)"
        prompt = _RECIPE_PROMPT.format(
            offering=(dossier.get("offering") or "")[:300],
            segments=seg_lines, need_signals=need_lines,
            geo=(dossier.get("geo") or "global")[:160], n=MAX_RECIPES,
        )
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        out = json.loads(raw)
    except Exception as e:
        logger.warning(f"[SignalEngine] recipe generation failed: {e}")
        return []

    recipes = []
    seen_labels = set()
    for r in out if isinstance(out, list) else []:
        if not isinstance(r, dict):
            continue
        label = _slug(str(r.get("label", "")))
        query = str(r.get("query", "")).strip()
        if not label or not query or label in seen_labels:
            continue
        seen_labels.add(label)
        try:
            weight = float(r.get("weight", 0.35))
        except (TypeError, ValueError):
            weight = 0.35
        recipes.append({
            "label": label,
            "query": query,
            "what_counts": str(r.get("what_counts", "")).strip()[:200],
            "weight": max(0.2, min(0.6, weight)),
        })
        if len(recipes) >= MAX_RECIPES:
            break
    logger.info(f"[SignalEngine] {len(recipes)} detectors: {[r['label'] for r in recipes]}")
    return recipes


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s[:40]


def _need_signals_fp(dossier: dict) -> str:
    """A fingerprint of the inputs recipes are derived from. When the dossier's need_signals
    or segments change, the fingerprint changes → recipes regenerate. Otherwise they're reused."""
    needs = sorted(str(n).strip().lower() for n in (dossier.get("need_signals") or []))
    segs = sorted(str(s.get("name", "")).strip().lower() for s in (dossier.get("core_segments") or []))
    return hashlib.sha256(json.dumps([needs, segs]).encode()).hexdigest()[:16]


async def _recipes_for_profile(profile_id: str, dossier: dict, sp: dict) -> list[dict]:
    """Return the profile's detector set, CACHED in search_profile.signal_recipes. This keeps
    labels STABLE across runs (required for v2's over-time per-company tally) and skips the Haiku
    generation call on every scan. Regenerates only when the dossier's need_signals/segments change."""
    from app.database import supabase
    from app.pipeline.query_builder import load_search_profile

    fp = _need_signals_fp(dossier)
    cached = sp.get("signal_recipes")
    if isinstance(cached, list) and cached and sp.get("signal_recipes_fp") == fp:
        logger.info(f"[SignalEngine] reusing {len(cached)} cached recipes")
        return cached

    recipes = await dossier_recipes(dossier)
    if recipes:
        try:
            # Re-read to avoid clobbering concurrent writes, then persist the recipe set + fp.
            sp2 = load_search_profile(profile_id) or dict(sp)
            sp2["signal_recipes"] = recipes
            sp2["signal_recipes_fp"] = fp
            supabase.table("user_profiles").update({"search_profile": sp2}).eq("id", profile_id).execute()
            logger.info(f"[SignalEngine] cached {len(recipes)} recipes for profile")
        except Exception as e:
            logger.warning(f"[SignalEngine] recipe cache write failed (using in-memory): {e}")
    return recipes


# ---------------------------------------------------------------------------
# 2. Generic Serper runner (one recipe -> recent articles)
# ---------------------------------------------------------------------------

def _is_recent(article: dict, max_days: int = 60) -> bool:
    """Mirror news_agent freshness parsing — a trigger is only a trigger if it's fresh."""
    raw = (article.get("date") or "").strip().lower()
    if not raw:
        return False
    try:
        if any(w in raw for w in ("hour", "minute", "just now")):
            return True
        if "day" in raw:
            return int(raw.split()[0]) <= max_days
        if "week" in raw:
            return int(raw.split()[0]) * 7 <= max_days
        if "month" in raw:
            return int(raw.split()[0]) <= max_days // 30
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
        parsed = datetime.strptime(raw, "%b %d, %Y").replace(tzinfo=timezone.utc)
        return parsed >= cutoff
    except Exception:
        return False


async def _search(recipe: dict) -> list[dict]:
    """One Serper News search for a detector; keep recent, non-junk articles."""
    if not SERPER_API_KEY:
        return []
    out = []
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                SERPER_NEWS_URL,
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": recipe["query"], "num": SERPER_NUM, "gl": "us", "hl": "en", "tbs": "qdr:m2"},
            )
            usage.log_serper()
            if resp.status_code != 200:
                logger.warning(f"[SignalEngine] Serper {resp.status_code} for '{recipe['query']}'")
                return out
            for a in resp.json().get("news", []):
                link = (a.get("link") or "").lower()
                if not link or any(d in link for d in _JUNK_DOMAINS):
                    continue
                if not _is_recent(a):
                    continue
                out.append(a)
    except Exception as e:
        logger.error(f"[SignalEngine] Serper search failed for '{recipe['query']}': {e}")
    return out


# ---------------------------------------------------------------------------
# 3. Verify + extract  (precision gate: does the article really evidence this need?)
# ---------------------------------------------------------------------------

_VERIFY_PROMPT = """You are checking news articles against ONE buying-signal detector.

DETECTOR: {label}
WHAT COUNTS: {what_counts}

For each article, decide if it PLAUSIBLY shows this kind of event at a specific, NAMED company
in a related space, then extract that company. This is a first-pass filter, not the final judge —
a later step scores fit strictly, so keep anything plausible.
  matches = true  → a specific company is named AND the article plausibly relates to this event.
  matches = false → ONLY if: it's a listicle / ranking / roundup with no single subject company,
                    OR no specific company can be identified, OR it's clearly a different, unrelated
                    industry. Do NOT reject just because the fit is imperfect or the company is small.

Articles:
{articles}

Return ONLY a JSON array, one object per article IN ORDER, no markdown:
[
  {{"matches": true, "company_name": "exact company or null",
    "summary": "one sentence: what happened and why it signals a need"}},
  ...
]"""


async def _verify_extract(recipe: dict, articles: list[dict]) -> list[dict]:
    """Confirm each article evidences the detector and pull the company. 5 per Haiku call.
    Returns signal dicts ready for the queue (company_name may be null → dropped)."""
    signals = []
    for i in range(0, len(articles), 5):
        batch = articles[i:i + 5]
        articles_text = "\n\n".join(
            f"Article {j+1}:\nTitle: {a.get('title','')}\nSnippet: {a.get('snippet','')}"
            for j, a in enumerate(batch)
        )
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=700,
                messages=[{"role": "user", "content": _VERIFY_PROMPT.format(
                    label=recipe["label"], what_counts=recipe["what_counts"] or recipe["label"],
                    articles=articles_text)}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            parsed = json.loads(raw)
        except Exception as e:
            logger.warning(f"[SignalEngine] verify failed for '{recipe['label']}': {e}")
            continue

        for j, a in enumerate(batch):
            if j >= len(parsed):
                break
            v = parsed[j]
            company = (v.get("company_name") or "").strip()
            if not v.get("matches") or not company:
                continue
            url = a.get("link", "")
            raw_text = f"{a.get('title','')}. {a.get('snippet','')}"
            signals.append({
                "signal_hash":     _hash(company, url),
                "signal_type":     recipe["label"],        # distinct type → stacks
                "company_name":    company,
                "company_domain":  None,
                "raw_text":        raw_text[:2000],
                "source_url":      url,
                "source_platform": "signal_engine",        # → leads_v2 skips vector gate
                "funding_amount":  None,
                "funding_round":   None,
                "summary":         (v.get("summary") or a.get("title", ""))[:300],
                "source_query":    recipe["query"],
                "detector_weight": recipe["weight"],       # metadata (scorer is authority)
            })
            if len(signals) >= MAX_PER_RECIPE:
                return signals
    return signals


def _hash(company: str, url: str) -> str:
    return hashlib.sha256(f"{company}{url}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# 4. Main run (called from leads_v2._run_agent_and_score, additive)
# ---------------------------------------------------------------------------

async def run(profile_id: str = None) -> dict:
    """Dossier need_signals → detectors → recent news → verified weak signals → queue.
    Fully self-contained; leads_v2 wraps this in try/except so a failure never kills a scan."""
    logger.info(f"[SignalEngine] Starting run (profile_id={profile_id})")
    if not profile_id:
        return {"skipped": "no profile_id"}

    from app.pipeline.query_builder import load_search_profile
    sp = load_search_profile(profile_id) or {}
    dossier = sp.get("dossier")
    if not dossier:
        logger.info("[SignalEngine] no dossier → skipping (engine needs a Seller Brain dossier)")
        return {"skipped": "no dossier"}

    recipes = await _recipes_for_profile(profile_id, dossier, sp)
    if not recipes:
        return {"detectors": 0, "signals_queued": 0}

    # Search all detectors concurrently.
    batches = await asyncio.gather(*[_search(r) for r in recipes])

    # Dedup articles by URL across ALL detectors so one article can't fire (and later
    # stack) under two labels — guard against same-event double-counting.
    seen_urls: set = set()
    per_recipe: list[tuple[dict, list[dict]]] = []
    for recipe, arts in zip(recipes, batches):
        fresh = []
        for a in arts:
            url = a.get("link", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            fresh.append(a)
        per_recipe.append((recipe, fresh))

    # Verify + extract per detector.
    all_signals: list[dict] = []
    per_label: dict = {}
    for recipe, arts in per_recipe:
        if not arts:
            continue
        sigs = await _verify_extract(recipe, arts)
        per_label[recipe["label"]] = len(sigs)
        all_signals.extend(sigs)
        if len(all_signals) >= MAX_TOTAL_SIGNALS:
            all_signals = all_signals[:MAX_TOTAL_SIGNALS]
            break

    # Push to the shared queue (queue hash-dedups against signals other agents pushed).
    queued = 0
    for s in all_signals:
        if await signal_queue.push(s):
            queued += 1

    logger.info(f"[SignalEngine] Done — {len(recipes)} detectors → {queued} signals queued ({per_label})")
    return {
        "detectors": len(recipes),
        "articles_found": sum(len(a) for _, a in per_recipe),
        "signals_queued": queued,
        "by_detector": per_label,
        "_queries": [r["query"] for r in recipes],
    }
