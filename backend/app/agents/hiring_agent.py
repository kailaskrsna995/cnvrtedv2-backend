"""
HIRING AGENT — the Intent-tab engine for service/agency sellers.
================================================================
For a SERVICE seller, literal stated intent ("recommend a studio") is a tiny pond.
The abundant, high-signal, on-modality form of intent is a company actively INVESTING
in the function the seller powers — and the clearest structured version is HIRING.

A company posting "Head of Content / Video Producer" is publicly declaring it is building
content capacity NOW → it needs production help. That's more concrete than a vague forum
post, plentiful, structured, and anti-bot-free.

DOSSIER-DRIVEN (generalizes to any seller): a Haiku step maps the seller's dossier →
the LEADERSHIP / COMMISSIONING job titles a buyer posts when investing in what the seller
sells (11FPS → Head of Content / Video Producer / Brand Lead; dev agency → Head of
Engineering / "website rebuild"; mktg agency → Head of Growth / CMO). We target buyers who
COMMISSION work, NOT junior bodies a company hires to do it in-house instead of outsourcing.

SOURCING: Serper web search, KEYWORD form ("{role}" hiring / careers) — NOT the `site:`
operator (free Serper rejects it). Filter organic results to ATS/job domains.

These signals are pushed as signal_type="hiring"; leads_v2 skips the vector gate for them
(short text) and tags them evidence_type="stated_intent" → they land in the Intent tab.
"""

import re
import json
import hashlib
import logging
import asyncio
import httpx
from urllib.parse import urlparse
from app.llm import Anthropic
from app import usage
from app.config import SERPER_API_KEY, ANTHROPIC_API_KEY
from app.queue import signal_queue
from app.database import supabase

logger = logging.getLogger(__name__)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

SERPER_SEARCH_URL = "https://google.serper.dev/search"

# ATS / job-board domains — an organic result on one of these is a real job posting,
# not a listicle or a "top companies hiring" blog.
JOB_DOMAINS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "jobs.",
    "careers.", "boards.greenhouse.io", "linkedin.com/jobs", "wellfound.com",
    "ycombinator.com/companies", "notion.so", "bamboohr.com", "recruitee.com",
    "workatastartup.com", "indeed.com", "glassdoor.com/job",
)

# Generic fallback roles if the dossier is missing (kept modality-agnostic).
_FALLBACK_ROLES = ["Head of Content", "Head of Marketing", "Head of Growth", "Brand Manager"]


# ---------------------------------------------------------------------------
# Dossier → commissioning job titles (the generalization step)
# ---------------------------------------------------------------------------

_ROLES_PROMPT = """A seller sells the offering below. When a company decides to INVEST in the
function this seller powers, it posts certain JOB ROLES. List the job titles that a company
would hire for that signal "we are building/scaling this function and will need outside help".

SELLER OFFERING: {offering}

RANKED TARGET SEGMENTS: {segments}

WHO BUYS (the seller's buyer titles): {buyer_titles}

Rules:
- Return LEADERSHIP / COMMISSIONING roles — the person who owns the function and hires vendors
  (e.g. "Head of Content", "VP Marketing", "Brand Director", "Head of Video").
- Do NOT return junior execution roles a company hires to do the work IN-HOUSE instead of
  outsourcing (e.g. "Junior Video Editor", "Motion Design Intern") — those REDUCE outsourcing.
- Use the seller's actual vertical vocabulary, not generic filler.
- 6-9 roles, each 2-4 words.

Return ONLY a JSON array of role-title strings."""


async def dossier_roles(dossier: dict) -> list[str]:
    """Map the dossier → the commissioning job titles that signal investment in the
    seller's function. Returns [] on failure so the caller can fall back."""
    if not dossier:
        return []
    try:
        segs = sorted(dossier.get("core_segments", []), key=lambda s: s.get("fit", 0), reverse=True)
        seg_lines = "; ".join(s.get("name", "") for s in segs[:5]) or "(none)"
        buyer_titles = ", ".join(dossier.get("buyer_titles") or []) or "(none)"
        prompt = _ROLES_PROMPT.format(
            offering=(dossier.get("offering") or "")[:300],
            segments=seg_lines, buyer_titles=buyer_titles,
        )
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        roles = [r for r in json.loads(raw) if isinstance(r, str) and r.strip()][:9]
        logger.info(f"[HiringAgent] {len(roles)} commissioning roles: {roles}")
        return roles
    except Exception as e:
        logger.warning(f"[HiringAgent] role mapping failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Serper job search (keyword form — free-tier safe, no site: operator)
# ---------------------------------------------------------------------------

# "Company is hiring a Head of X" / "Head of X at Company" — title fallback extraction.
_AT_RE = re.compile(r"\bat\s+([A-Z][\w&.\- ]{2,40})", re.IGNORECASE)
_HIRING_RE = re.compile(r"^([A-Z][\w&.\- ]{2,40}?)\s+(?:is\s+)?hiring", re.IGNORECASE)

# Listing/search/blog pages (not a specific posting) — drop them.
_LISTING_MARKERS = ("/search", "/category/", "indeed.com/q-", "/jobs.html",
                    "apple.com/en-us/search", "/location/", "career-advice",
                    "view-all-jobs", "/go/", "governmentjobs.com")
# A real posting carries a job id in the URL (e.g. R000105796, 4431966692, JREQ200835);
# homepages/listings/blog articles don't. This positive gate drops most non-postings.
_JOB_ID_RE = re.compile(r"\d{4,}")


def _prettify(slug: str) -> str:
    s = re.sub(r"[-_]+", " ", slug).strip()
    return s.upper() if (len(s) <= 4 and " " not in s) else s.title()


def _company_from_url(url: str) -> str | None:
    """ATS URLs embed the company slug — far more reliable than parsing the title.
    boards.greenhouse.io/{co}, jobs.lever.co/{co}, careers.{co}.com, {co}.recruitee.com …"""
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        segs = [s for s in p.path.split("/") if s]
        for ats in ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "recruitee.com"):
            if ats in host and segs and segs[0] not in ("jobs", "search", "en", "en-us"):
                return _prettify(segs[0])
        parts = host.split(".")
        # company as subdomain: careers.{co}.com / jobs.{co}.com / apply.{co}.com
        if parts[0] in ("careers", "jobs", "job", "apply", "boards", "work") and len(parts) >= 3:
            return _prettify(parts[1])
        if host.endswith("recruitee.com") and len(parts) >= 3:
            return _prettify(parts[0])
        return None
    except Exception:
        return None


def _extract_company(url: str, title: str) -> str | None:
    co = _company_from_url(url)
    if co:
        return co
    for rx in (_HIRING_RE, _AT_RE):
        m = rx.search(title or "")
        if m:
            name = m.group(1).strip(" -–|,·")
            if 2 < len(name) < 45:
                return name
    return None


async def search_jobs(role: str, query: str) -> list[dict]:
    """One Serper web search for a role's job postings; keep only ATS/job-domain results,
    drop listing/search pages, and pull the company from the ATS URL."""
    if not SERPER_API_KEY:
        return []
    out = []
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            # NOTE: free Serper rejects phrase-match quotes AND site: (400 "query pattern
            # not allowed") — so use plain keywords + qdr:m (proven buyer_intent pattern),
            # then filter organic results to ATS/job domains.
            resp = await http.post(
                SERPER_SEARCH_URL,
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": 20, "tbs": "qdr:m"},
            )
            usage.log_serper()
            if resp.status_code != 200:
                logger.warning(f"[HiringAgent] Serper {resp.status_code} for '{query}'")
                return out
            for r in resp.json().get("organic", []):
                link = r.get("link", "")
                low = link.lower()
                if not any(d in link for d in JOB_DOMAINS):
                    continue
                if any(m in low for m in _LISTING_MARKERS) or low.rstrip("/").endswith("/jobs"):
                    continue   # listing/search/blog page, not a specific posting
                if not _JOB_ID_RE.search(urlparse(link).path):
                    continue   # no job id in the path → homepage/listing, not a real posting
                title = r.get("title", "")
                out.append({
                    "role": role,
                    "title": title,
                    "snippet": r.get("snippet", "")[:500],
                    "url": link,
                    "company": _extract_company(link, title),
                    "query": query,
                })
    except Exception as e:
        logger.error(f"[HiringAgent] Serper search failed for '{query}': {e}")
    return out


def _vertical_terms(sp: dict, dossier: dict) -> list[str]:
    """The seller's vertical nouns — anchor each role query to these so a bare 'VP' query
    doesn't pull BlackRock/Citi. industry_terms first, then dossier segment names."""
    terms = list(sp.get("industry_terms") or [])
    if not terms and dossier:
        terms = [s.get("name", "") for s in (dossier.get("core_segments") or [])]
    return [t for t in terms if t][:3]


def make_signal_hash(url: str) -> str:
    return hashlib.sha256((url or "").encode()).hexdigest()


# ---------------------------------------------------------------------------
# Main run (called from leads_v2.py per profile)
# ---------------------------------------------------------------------------

async def run(profile_id: str = None) -> dict:
    logger.info(f"[HiringAgent] Starting run (profile_id={profile_id})")

    # 1. Build roles + vertical anchors from the dossier.
    roles: list[str] = []
    sp: dict = {}
    dossier = None
    if profile_id:
        from app.pipeline.query_builder import load_search_profile
        sp = load_search_profile(profile_id) or {}
        dossier = sp.get("dossier")
        roles = await dossier_roles(dossier)
        if not roles:
            roles = [t for t in ((dossier or {}).get("buyer_titles") or []) if t][:8]
    if not roles:
        roles = _FALLBACK_ROLES
        logger.info(f"[HiringAgent] using fallback roles: {roles}")

    verticals = _vertical_terms(sp, dossier)

    # 2. Vertical-anchor each role query (role × vertical) so results stay in the seller's
    # market — a bare "VP Originals" query pulls BlackRock/Citi; "VP Originals streaming"
    # pulls the right companies. Fall back to bare role queries if no vertical is known.
    specs: list[tuple[str, str]] = []
    for role in roles[:6]:
        if verticals:
            for v in verticals[:2]:
                specs.append((role, f"{role} {v} jobs hiring"))
        else:
            specs.append((role, f"{role} careers hiring"))
    specs = specs[:12]
    logger.info(f"[HiringAgent] {len(specs)} queries (verticals={verticals})")

    batches = await asyncio.gather(*[search_jobs(role, q) for role, q in specs])
    posts = [p for b in batches for p in b]
    logger.info(f"[HiringAgent] {len(posts)} job posts across {len(specs)} queries")

    # 3. Dedup by URL, push to the shared queue.
    seen: set = set()
    queued = 0
    for p in posts:
        url = p.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        company = p.get("company")
        raw = (f"{company} is hiring: {p['role']}. " if company else f"Hiring {p['role']}: {p['title']}. ") + p["snippet"]
        signal = {
            "signal_hash":     make_signal_hash(url),
            "signal_type":     "hiring",
            "company_name":    company,          # may be None → scoring extracts it
            "company_domain":  None,
            "raw_text":        raw[:2000],
            "source_url":      url,
            "source_platform": "serper_jobs",
            "funding_amount":  None,
            "funding_round":   None,
            "summary":         f"Hiring: {p['role']}" + (f" — {company}" if company else ""),
            "source_query":    p.get("query", ""),
        }
        await signal_queue.push(signal)
        queued += 1

    logger.info(f"[HiringAgent] Done — {len(posts)} posts → {queued} queued")
    return {
        "roles": len(roles),
        "posts_found": len(posts),
        "signals_queued": queued,
        "_queries": [q for _, q in specs],
    }
