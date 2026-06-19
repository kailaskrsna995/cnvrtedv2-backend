"""
LINKEDIN AGENT (cookieless, via Apify)
======================================
Two capabilities:

  1. PEOPLE SEARCH  — find people matching the ICP (titles + industry + geo)
     → "people you could reach out to" cold list

  2. CONTACT FINDER — given a company we already surfaced as a lead, find a
     relevant decision-maker among its employees → who to actually contact

Both use cookieless Apify actors (no LinkedIn session, no ban risk):
  people    : powerai/linkedin-peoples-search-scraper
  employees : apimaestro/linkedin-company-employees-scraper-no-cookies

Actors are pay-per-event — keep maxResults small.
"""

import logging
import httpx
from app.config import (
    APIFY_API_TOKEN, APIFY_PEOPLE_SEARCH_ACTOR, APIFY_EMPLOYEES_ACTOR,
)

logger = logging.getLogger(__name__)

# Decision-maker titles agencies want to reach (ranked best-first)
TARGET_TITLES = [
    "Chief Marketing Officer", "Head of Marketing", "VP Marketing",
    "Head of Content", "Head of Growth", "Founder", "CEO", "Marketing Director",
]


def _actor_url(actor: str) -> str:
    return f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"


async def _run_actor(actor: str, payload: dict, timeout: float = 90) -> list[dict]:
    """Run an Apify actor synchronously and return dataset items."""
    if not APIFY_API_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(
                _actor_url(actor),
                params={"token": APIFY_API_TOKEN},
                json=payload,
            )
            if resp.status_code not in (200, 201):
                logger.warning(f"[LinkedIn] {actor} {resp.status_code}: {resp.text[:150]}")
                return []
            return resp.json()
    except Exception as e:
        logger.error(f"[LinkedIn] {actor} failed: {e}")
        return []


def _norm_person(item: dict) -> dict:
    """Normalize varied actor output into a consistent person shape."""
    loc = item.get("location") or item.get("geo") or ""
    if isinstance(loc, dict):
        loc = loc.get("full") or loc.get("city") or loc.get("country") or ""
    return {
        "name": item.get("name") or item.get("full_name") or item.get("fullName") or
                f"{item.get('first_name','')} {item.get('last_name','')}".strip() or "Unknown",
        "title": item.get("title") or item.get("headline") or item.get("jobTitle") or "",
        "company": item.get("company") or item.get("current_company") or
                   item.get("companyName") or "",
        "profile_url": item.get("url") or item.get("profileUrl") or
                       item.get("profile_url") or item.get("linkedinUrl") or
                       (f"https://linkedin.com/in/{item.get('public_identifier')}" if item.get("public_identifier") else ""),
        "location": loc,
    }


# ---------------------------------------------------------------------------
# 1. People search — cold list
# ---------------------------------------------------------------------------

async def search_people(profile_id: str, max_results: int = 20) -> list[dict]:
    """Find people matching the ICP facets. Returns a cold outreach list."""
    if not APIFY_API_TOKEN:
        return []

    from app.pipeline.query_builder import load_search_profile
    sp = load_search_profile(profile_id) or {}

    titles = TARGET_TITLES[:3]
    industries = sp.get("industry_terms", [])[:2]

    people = []
    # one search per title; bias toward the core industry via the name keyword.
    # NOTE: geocode_location expects a numeric geo ID, not a country string —
    # omitting it (passing a string causes the actor to 503).
    for title in titles:
        payload = {
            "title": title,
            "maxResults": max(5, max_results // len(titles)),
        }
        if industries:
            payload["company"] = industries[0]
        items = await _run_actor(APIFY_PEOPLE_SEARCH_ACTOR, payload)
        for it in items:
            p = _norm_person(it)
            if p["profile_url"]:
                people.append(p)

    # dedupe by profile url
    seen, unique = set(), []
    for p in people:
        if p["profile_url"] not in seen:
            seen.add(p["profile_url"])
            unique.append(p)
    logger.info(f"[LinkedIn] People search: {len(unique)} people")
    return unique[:max_results]


# ---------------------------------------------------------------------------
# 2. Contact finder — who to reach at a company lead
# ---------------------------------------------------------------------------

# Keywords that confirm a returned person is actually a decision-maker
DM_KEYWORDS = [
    "marketing", "content", "growth", "brand", "founder", "ceo", "chief",
    "cmo", "vp", "vice president", "head", "director", "lead", "manager",
    "partnerships", "demand", "revenue", "gtm", "communications",
]
# Titles that disqualify (junk matches from loose actor filters)
DM_REJECT = ["student", "intern", "trainee", "freelance", "seeking", "looking for"]


def _is_decision_maker(title: str) -> bool:
    t = (title or "").lower()
    if not t or any(bad in t for bad in DM_REJECT):
        return False
    return any(k in t for k in DM_KEYWORDS)


# Title seniority ranking — higher = better contact for an agency pitch
def _title_rank(title: str) -> int:
    t = (title or "").lower()
    if any(k in t for k in ["cmo", "chief marketing", "chief content", "chief growth"]): return 100
    if "founder" in t or "ceo" in t or "chief executive" in t: return 90
    if "vp" in t or "vice president" in t: return 80
    if "head of" in t: return 70
    if "director" in t: return 60
    if "lead" in t or "manager" in t: return 40
    if any(k in t for k in ["marketing", "content", "growth", "brand", "partnerships"]): return 30
    return 10


def _slug_candidates(company_name: str, domain: str = None) -> list[str]:
    """LinkedIn company-slug guesses, tried in order. Actor needs the slug."""
    import re
    cands = []
    # domain root is often the slug (voxa.com -> voxa)
    if domain:
        root = re.sub(r"\.(com|io|ai|co|app|net|org).*$", "", domain.lower().replace("www.", ""))
        root = root.split(".")[-1] if "." in root else root
        if root:
            cands.append(root)
    name = company_name.strip()
    cands.append(name)                                   # raw (works when name == slug)
    cands.append(re.sub(r"[^a-z0-9]", "", name.lower())) # "Kuku FM" -> "kukufm"
    cands.append(re.sub(r"\s+", "-", name.lower()))      # "Josh Talks" -> "josh-talks"
    # dedupe preserving order
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c); out.append(c)
    return out


async def find_contact(company_name: str, company_domain: str = None) -> dict | None:
    """Find the most senior marketing/content person at a company."""
    if not APIFY_API_TOKEN or not company_name:
        return None

    for ident in _slug_candidates(company_name, company_domain)[:3]:
        items = await _run_actor(APIFY_EMPLOYEES_ACTOR, {
            "identifier": ident,
            "max_employees": 15,
            "job_title": "marketing",
        }, timeout=75)
        candidates = []
        for it in items:
            p = _norm_person(it)
            if p["profile_url"] and _is_decision_maker(p["title"]):
                p["_rank"] = _title_rank(p["title"])
                candidates.append(p)
        if candidates:
            best = max(candidates, key=lambda x: x["_rank"])
            logger.info(f"[LinkedIn] Contact for {company_name} (via '{ident}'): {best['name']} ({best['title']})")
            return best

    logger.info(f"[LinkedIn] No clear decision-maker found for {company_name}")
    return None


async def enrich_leads_with_contacts(leads: list[dict], max_lookups: int = 8) -> int:
    """
    Attach a contact person to the top company leads (in place).
    Capped to control Apify cost. Returns number enriched.
    """
    if not APIFY_API_TOKEN:
        return 0
    enriched = 0
    for lead in leads[:max_lookups]:
        name = lead.get("company_name")
        if not name:
            continue
        contact = await find_contact(name, lead.get("company_domain"))
        if contact:
            lead["contact_name"] = contact["name"]
            lead["contact_title"] = contact["title"]
            lead["contact_linkedin"] = contact["profile_url"]
            enriched += 1
    logger.info(f"[LinkedIn] Enriched {enriched} leads with contacts")
    return enriched
