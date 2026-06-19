"""
ENRICHMENT WATERFALL
====================
Finds the decision maker's contact details for a company.
Tries services in order, stops when one succeeds.

Order:
  1. Check enrichment_cache (free — already paid for this company)
  2. Hunter.io       — finds email from domain
  3. FullEnrich      — email + phone + LinkedIn
  4. Apollo          — broader fallback
  5. Partial save    — company only, no contact (better than nothing)

Key rule: cache per company domain FOREVER (30 day TTL then re-verify).
Never pay for the same company twice.
"""

import logging
import httpx
from app.database import supabase
from app.config import HUNTER_API_KEY, FULLENRICH_API_KEY, APOLLO_API_KEY

logger = logging.getLogger(__name__)


async def enrich_company(company_domain: str, company_name: str = "") -> dict:
    """
    Main entry point. Returns enrichment dict or partial result.
    { decision_maker, title, email, phone, linkedin_url, source }
    """
    if not company_domain:
        return _empty()

    # 1. Check cache first
    cached = _check_cache(company_domain)
    if cached:
        logger.info(f"[Enrichment] Cache hit for {company_domain}")
        return cached

    # 2. Try waterfall
    result = (
        await _try_hunter(company_domain)
        or await _try_fullenrich(company_domain)
        or await _try_apollo(company_domain, company_name)
        or _empty()
    )

    # 3. Save to cache regardless (even empty = we tried)
    _save_cache(company_domain, company_name, result)
    return result


def _check_cache(domain: str) -> dict | None:
    """Returns cached enrichment if it exists and hasn't expired."""
    try:
        result = supabase.table("enrichment_cache") \
            .select("*") \
            .eq("company_domain", domain) \
            .gt("expires_at", "now()") \
            .execute()
        if result.data:
            row = result.data[0]
            return {
                "decision_maker": row.get("decision_maker"),
                "title":          row.get("title"),
                "email":          row.get("email"),
                "phone":          row.get("phone"),
                "linkedin_url":   row.get("linkedin_url"),
                "source":         row.get("source"),
            }
    except Exception as e:
        logger.warning(f"[Enrichment] Cache check failed: {e}")
    return None


def _save_cache(domain: str, company_name: str, data: dict):
    try:
        supabase.table("enrichment_cache").upsert({
            "company_domain":  domain,
            "company_name":    company_name,
            "decision_maker":  data.get("decision_maker"),
            "title":           data.get("title"),
            "email":           data.get("email"),
            "phone":           data.get("phone"),
            "linkedin_url":    data.get("linkedin_url"),
            "source":          data.get("source"),
        }, on_conflict="company_domain").execute()
    except Exception as e:
        logger.warning(f"[Enrichment] Cache save failed: {e}")


async def _try_hunter(domain: str) -> dict | None:
    """Hunter.io domain search — finds email from company domain."""
    if not HUNTER_API_KEY:
        return None
    # TODO: implement Hunter.io API call
    # GET https://api.hunter.io/v2/domain-search?domain={domain}&api_key={key}
    return None


async def _try_fullenrich(domain: str) -> dict | None:
    """FullEnrich — email + phone + LinkedIn."""
    if not FULLENRICH_API_KEY:
        return None
    # TODO: implement FullEnrich API call
    return None


async def _try_apollo(domain: str, company_name: str) -> dict | None:
    """Apollo.io people search — broader fallback."""
    if not APOLLO_API_KEY:
        return None
    # TODO: implement Apollo people search
    # POST https://api.apollo.io/v1/mixed_people/search
    return None


def _empty() -> dict:
    return {
        "decision_maker": None,
        "title":          None,
        "email":          None,
        "phone":          None,
        "linkedin_url":   None,
        "source":         "none",
    }
