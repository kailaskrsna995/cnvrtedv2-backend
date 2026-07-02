"""
APOLLO CONTACT ENRICHMENT
=========================
company domain + buyer titles → the most-senior matching point-of-contact:
{name, title, email, linkedin}. One provider for BOTH email + LinkedIn.

Flow (on-demand only — wired to the "Find contacts" button, NEVER auto during a scan):
  1. People Search by domain + buyer titles → candidates (name, title, linkedin_url, id).
  2. Pick the most senior by title.
  3. Enrich/match to REVEAL the work email (this is the credit-consuming step).
"""

import logging
import re
import httpx
from app.config import APOLLO_API_KEY
from app import usage

logger = logging.getLogger(__name__)
BASE = "https://api.apollo.io/api/v1"

# Title seniority — higher rank = more senior decision-maker.
_SENIORITY = ["founder", "ceo", "chief", "president", "vp", "vice president",
              "head", "director", "lead", "principal", "senior manager", "manager"]


def _rank(title: str) -> int:
    t = (title or "").lower()
    for i, w in enumerate(_SENIORITY):
        if w in t:
            return len(_SENIORITY) - i
    return 0


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


async def find_contact(company_name: str, domain: str, titles: list[str]) -> dict | None:
    """Return the best POC for a company, or None. Reveals one work email (1 credit)."""
    if not APOLLO_API_KEY or not domain:
        return None
    headers = {"X-Api-Key": APOLLO_API_KEY, "Content-Type": "application/json", "Cache-Control": "no-cache"}
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            # 1. People search by domain + buyer titles
            search = await http.post(f"{BASE}/mixed_people/api_search", headers=headers, json={
                "person_titles": (titles or [])[:12],
                "q_organization_domains_list": [domain],
                "page": 1,
                "per_page": 10,
            })
            if search.status_code != 200:
                logger.warning(f"[apollo] search {search.status_code}: {search.text[:200]}")
                return None
            people = search.json().get("people") or search.json().get("contacts") or []
            if not people:
                logger.info(f"[apollo] no people for {domain}")
                return None
            people.sort(key=lambda p: _rank(p.get("title")), reverse=True)
            top = people[0]

            email = _clean_email(top.get("email"))
            phone = _extract_phone(top)
            # 2. reveal work email if locked (1 email credit). We do NOT pass
            #    reveal_phone_number — Apollo 400s it without a webhook_url (mobiles
            #    are delivered async). Phone stays best-effort from the response.
            if not email and top.get("id"):
                match = await http.post(f"{BASE}/people/match", headers=headers, json={
                    "id": top["id"],
                    "reveal_personal_emails": False,   # work email only
                })
                if match.status_code == 200:
                    try: usage.log_apollo_reveal()
                    except Exception: pass
                    person = match.json().get("person", {}) or {}
                    email = email or _clean_email(person.get("email"))
                    phone = phone or _extract_phone(person)
                    if not top.get("linkedin_url"):
                        top["linkedin_url"] = person.get("linkedin_url")
                else:
                    logger.warning(f"[apollo] match {match.status_code}: {match.text[:160]}")

            name = top.get("name") or f"{top.get('first_name','')} {top.get('last_name','')}".strip()
            return {
                "name": name or None,
                "title": top.get("title"),
                "email": email,
                "phone": phone,
                "linkedin": top.get("linkedin_url"),
            }
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
