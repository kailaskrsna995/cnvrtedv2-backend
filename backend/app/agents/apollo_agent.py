"""
APOLLO CONTACT ENRICHMENT
=========================
company domain + buyer titles → the best-fit point-of-contact for cold outreach — the
FUNCTIONAL OWNER who actually replies, not the CEO — {name, title, email, linkedin}.
One provider for BOTH email + LinkedIn.

Flow (on-demand only — wired to the "Find contacts" button, NEVER auto during a scan):
  1. People Search by domain + buyer titles → candidates (name, title, linkedin_url, id).
  2. Rank by CHAMPION fit (Head/Director/Manager owns the budget + replies; C-suite = fallback).
  3. Enrich/match to REVEAL the work email (this is the credit-consuming step).
"""

import logging
import re
import httpx
from app.config import APOLLO_API_KEY
from app import usage

logger = logging.getLogger(__name__)
BASE = "https://api.apollo.io/api/v1"

# Cold-outreach RESPONDER sweet spot (persona-aware, NOT raw seniority). The person who
# actually reads a cold email, owns the relevant budget, and routes it is the FUNCTIONAL
# OWNER — Head / Director / Manager / Lead of the function — not the CEO (who delegates and
# won't reply) and not a junior IC (no budget). So we rank the champion band highest, VP
# just below, and keep the C-suite only as a FALLBACK (the right answer at a tiny company
# with no functional layer). Higher = better cold-outreach target.
_CSUITE_ABBR = {"ceo", "cmo", "cto", "coo", "cfo", "cro", "cco", "cpo", "cxo"}  # whole-word only
_JUNIOR = ("coordinator", "associate", "assistant", "intern", "trainee", "analyst")


def _rank(title: str) -> int:
    """Champion fit for cold outreach (higher = better target). C-suite abbreviations are
    matched as whole WORDS — substring would wrongly catch e.g. 'coo' inside 'coordinator'."""
    t = (title or "").strip().lower()
    if not t:
        return 3
    words = set(re.split(r"[^a-z]+", t))
    if t.startswith("head") or "head of" in t or "director" in t:
        return 6                                    # functional owner — best
    if any(w in t for w in ("manager", "lead", "principal")):
        return 5                                    # owns/executes the function, replies
    if "vp" in t or "vice president" in t:
        return 4                                    # senior functional, less responsive
    if (words & _CSUITE_ABBR) or any(w in t for w in ("chief", "founder", "president", "owner", "partner")):
        return 2                                    # C-suite — reachable FALLBACK only
    if any(w in t for w in _JUNIOR):
        return 1                                    # too junior — no budget
    return 3                                        # unknown/other — neutral


def _clean_email(e: str) -> str | None:
    if not e or "not_unlocked" in e or "email_not_unlocked" in e or "domain.com" == e.split("@")[-1]:
        return None
    return e


def _extract_phone(p: dict) -> str | None:
    """Best phone available on an Apollo person: revealed mobile/direct first,
    then any listed number, then the org's number. (Mobile reveal needs
    reveal_phone_number=True on the match + may arrive async; we take whatever
    the synchronous response carries.)"""
    if not p:
        return None
    for n in (p.get("phone_numbers") or []):
        v = n.get("sanitized_number") or n.get("raw_number")
        if v:
            return v
    org = p.get("organization") or {}
    return (p.get("sanitized_phone") or org.get("sanitized_phone")
            or org.get("phone") or org.get("primary_phone", {}).get("number") or None)


async def _resolve_domain(http, headers, company_name: str) -> str | None:
    """No domain? Look one up from the company name via Apollo org search (no new provider).
    Name-only leads (precision/watchlist) used to be skipped entirely — this recovers them."""
    if not company_name:
        return None
    try:
        r = await http.post(f"{BASE}/mixed_companies/api_search", headers=headers, json={
            "q_organization_name": company_name, "page": 1, "per_page": 1,
        })
        if r.status_code == 200:
            orgs = r.json().get("organizations") or r.json().get("accounts") or []
            if orgs:
                d = orgs[0].get("primary_domain") or orgs[0].get("website_url") or ""
                d = re.sub(r"^https?://(www\.)?", "", d).split("/")[0].strip()
                return d or None
    except Exception as e:
        logger.debug(f"[apollo] domain resolve failed for {company_name}: {e}")
    return None


async def find_contact(company_name: str, domain: str, titles: list[str]) -> dict | None:
    """Best POC for a company, or None. Maximizes Apollo coverage: resolves a domain if
    missing, broadens titles if the filtered search is empty, and walks candidates
    (champion first) revealing until one yields a work email (reveals capped at 3 = cost bound)."""
    if not APOLLO_API_KEY:
        return None
    headers = {"X-Api-Key": APOLLO_API_KEY, "Content-Type": "application/json", "Cache-Control": "no-cache"}
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            # 0. Resolve a domain if we don't have one (was the #1 cause of misses).
            if not domain and company_name:
                domain = await _resolve_domain(http, headers, company_name)
            if not domain:
                return None

            # 1. People search by domain + titles; if empty, retry WITHOUT titles
            #    (any decision-maker at the domain) so a title mismatch doesn't zero us out.
            async def _search(with_titles: bool) -> list:
                body = {"q_organization_domains_list": [domain], "page": 1, "per_page": 10}
                if with_titles and titles:
                    body["person_titles"] = titles[:12]
                r = await http.post(f"{BASE}/mixed_people/api_search", headers=headers, json=body)
                if r.status_code != 200:
                    logger.warning(f"[apollo] search {r.status_code}: {r.text[:160]}")
                    return []
                return r.json().get("people") or r.json().get("contacts") or []

            people = await _search(with_titles=True) or await _search(with_titles=False)
            if not people:
                logger.info(f"[apollo] no people for {domain}")
                return None
            people.sort(key=lambda p: _rank(p.get("title")), reverse=True)

            # 2. Walk candidates CHAMPION-first (functional owner > VP > C-suite fallback);
            #    reveal until one has a work email. reveals capped at 3 → bounds credit cost.
            best = None
            reveals = 0
            for p in people[:6]:
                email = _clean_email(p.get("email"))
                phone = _extract_phone(p)
                if not email and p.get("id") and reveals < 3:
                    reveals += 1
                    match = await http.post(f"{BASE}/people/match", headers=headers, json={
                        "id": p["id"], "reveal_personal_emails": False,   # work email only
                    })
                    if match.status_code == 200:
                        try: usage.log_apollo_reveal()
                        except Exception: pass
                        person = match.json().get("person", {}) or {}
                        email = _clean_email(person.get("email"))
                        phone = phone or _extract_phone(person)
                        if not p.get("linkedin_url"):
                            p["linkedin_url"] = person.get("linkedin_url")
                    else:
                        logger.warning(f"[apollo] match {match.status_code}: {match.text[:160]}")
                cand = {
                    "name": p.get("name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip() or None,
                    "title": p.get("title"),
                    "email": email,
                    "phone": phone,
                    "linkedin": p.get("linkedin_url"),
                }
                if email:
                    return cand              # reachable contact found → done
                best = best or cand          # keep the top champion as a fallback (name/title/linkedin)
            return best                      # no email anywhere → still return the best-fit person
    except Exception as e:
        logger.error(f"[apollo] find_contact failed for {domain}: {e}")
        return None


def linkedin_profile_from_post(url: str) -> str | None:
    """Derive the AUTHOR's profile URL from a LinkedIn post/permalink:
    linkedin.com/posts/{slug}_... → linkedin.com/in/{slug}. Returns None when the
    URL carries no author slug (e.g. /feed/update/urn:li:activity:...)."""
    if not url:
        return None
    m = re.search(r"linkedin\.com/posts/([^_/?#]+)_", url)
    if m:
        return f"https://www.linkedin.com/in/{m.group(1)}"
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url)
    if m:
        return f"https://www.linkedin.com/in/{m.group(1)}"
    return None


async def find_person_by_linkedin(linkedin_url: str) -> dict | None:
    """Match a specific person by their LinkedIn profile URL and reveal their work
    email — used to find the AUTHOR of a LinkedIn intent post. Reveals 1 email credit."""
    if not APOLLO_API_KEY or not linkedin_url:
        return None
    headers = {"X-Api-Key": APOLLO_API_KEY, "Content-Type": "application/json", "Cache-Control": "no-cache"}
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            match = await http.post(f"{BASE}/people/match", headers=headers, json={
                "linkedin_url": linkedin_url,
                "reveal_personal_emails": False,   # work email only
            })
            if match.status_code != 200:
                logger.warning(f"[apollo] linkedin match {match.status_code}: {match.text[:160]}")
                return None
            try: usage.log_apollo_reveal()
            except Exception: pass
            person = match.json().get("person") or {}
            if not person:
                return None
            email = _clean_email(person.get("email"))
            name = person.get("name") or f"{person.get('first_name','')} {person.get('last_name','')}".strip()
            title = person.get("title")
            org = (person.get("organization") or {}).get("name")
            if title and org and org.lower() not in title.lower():
                title = f"{title}, {org}"
            elif not title and org:
                title = org
            return {
                "name": name or None,
                "title": title,
                "email": email,
                "phone": _extract_phone(person),
                "linkedin": person.get("linkedin_url") or linkedin_url,
            }
    except Exception as e:
        logger.error(f"[apollo] find_person_by_linkedin failed for {linkedin_url}: {e}")
        return None
