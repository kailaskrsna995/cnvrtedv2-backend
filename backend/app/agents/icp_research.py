"""
ICP RESEARCH — gather real evidence before generating an ICP
============================================================
Turns ICP generation from a one-shot homepage read into evidence-based
research. All free (Exa free tier + Serper).

Evidence gathered:
  1. Lookalike companies   — Exa neural search for companies in the same space
  2. Third-party reviews   — G2/Capterra/Product Hunt snippets (real buyer
                             roles + pain language, in customers' own words)
  3. (named customers come from the deep crawl, extracted at synthesis time)

Output is a compact evidence dict injected into the ICP generation prompt.
"""

import logging
import httpx
from app.config import EXA_API_KEY, SERPER_API_KEY
from app import usage

logger = logging.getLogger(__name__)
SERPER_SEARCH_URL = "https://google.serper.dev/search"


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


async def find_lookalikes(website_url: str, service_desc: str) -> list[dict]:
    """Exa: companies similar to this one (same space / likely same buyers)."""
    if not EXA_API_KEY:
        return []
    out = []
    try:
        from app.exa_client import Exa
        exa = Exa(api_key=EXA_API_KEY)
        own = _domain_of(website_url)
        # describe-the-space query beats raw findSimilar (which returned own subpages)
        query = f"companies similar to {service_desc[:200]}"
        r = exa.search(query, type="auto", num_results=12,
                       exclude_domains=[own] if own else None)
        for x in r.results:
            d = _domain_of(x.url or "")
            if d and d != own:
                out.append({"name": (x.title or d).split("|")[0].strip()[:60], "url": x.url})
        # dedupe by domain
        seen, uniq = set(), []
        for c in out:
            dom = _domain_of(c["url"])
            if dom not in seen:
                seen.add(dom); uniq.append(c)
        logger.info(f"[ICPResearch] {len(uniq)} lookalikes")
        return uniq[:10]
    except Exception as e:
        logger.warning(f"[ICPResearch] lookalikes failed: {e}")
        return []


async def find_reviews(company_name: str, service_desc: str = "") -> list[str]:
    """Serper: pull review-site snippets — real buyer roles + pain words.
    Biases the query with a service keyword to reduce namesake collisions."""
    if not SERPER_API_KEY or not company_name:
        return []
    # grab 2-3 distinguishing keywords from the service description
    kw = " ".join(w for w in service_desc.split()[:6] if len(w) > 3)[:60]
    snippets = []
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            for site in ["g2.com", "capterra.com", "producthunt.com"]:
                resp = await http.post(
                    SERPER_SEARCH_URL,
                    headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                    json={"q": f"site:{site} {company_name} {kw} reviews", "num": 4},
                )
                usage.log_serper()
                if resp.status_code != 200:
                    continue
                for r in resp.json().get("organic", []):
                    s = r.get("snippet", "")
                    if s:
                        snippets.append(f"[{site}] {s}")
        logger.info(f"[ICPResearch] {len(snippets)} review snippets")
        return snippets[:10]
    except Exception as e:
        logger.warning(f"[ICPResearch] reviews failed: {e}")
        return []


async def populate_competitors(profile_id: str, website_url: str, service_desc: str) -> int:
    """Find competitors (Exa lookalikes) and store them for a profile."""
    from app.database import supabase
    lookalikes = await find_lookalikes(website_url, service_desc)
    rows = [{"profile_id": profile_id, "name": c["name"], "url": c.get("url")}
            for c in lookalikes if c.get("name")]
    if rows:
        supabase.table("competitors").upsert(rows, on_conflict="profile_id,name").execute()
    logger.info(f"[ICPResearch] stored {len(rows)} competitors for {profile_id}")
    return len(rows)


async def research_company(website_url: str, company_name: str, service_desc: str) -> dict:
    """Gather evidence for ICP. Lookalikes only — reviews dropped: name-based
    review search collides with namesakes (wrong company) and misleads the ICP.
    Lookalikes (Exa, anchored on the actual site + service) are reliable."""
    lookalikes = await find_lookalikes(website_url, service_desc)
    return {"lookalikes": lookalikes, "reviews": []}


def format_evidence(research: dict) -> str:
    """Render the evidence block for the ICP prompt."""
    if not research:
        return ""
    parts = []
    if research.get("lookalikes"):
        names = ", ".join(c["name"] for c in research["lookalikes"])
        parts.append(f"SIMILAR COMPANIES (same space, likely same buyer base): {names}")
    if research.get("reviews"):
        parts.append("THIRD-PARTY REVIEW SNIPPETS (real buyer language — mine for actual buyer roles, company types, pain points. CAUTION: verify these describe the SAME company as the website; ignore any that look like a different namesake product):\n" + "\n".join(f"- {s}" for s in research["reviews"]))
    return "\n\n".join(parts)
