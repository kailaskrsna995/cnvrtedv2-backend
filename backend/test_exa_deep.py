"""Standalone Exa deep-vs-auto test for the 11fps dossier queries.
Small scale (2 queries, 8 results) to keep credit spend low. Prints the companies
each mode returns so we can judge whether 'deep' surfaces better on-ICP names
(Pratilipi / Rusk-class) before wiring it into the precision agent.
"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8")
from urllib.parse import urlparse
from app.config import EXA_API_KEY
from app.database import supabase
from app.agents.watchlist_agent import _JUNK_DOMAINS
from exa_py import Exa

FPS = "f52ba30f-3de8-41ed-9f5f-abdad1de26e9"
exa = Exa(api_key=EXA_API_KEY)

p = supabase.table("user_profiles").select("search_profile").eq("id", FPS).execute()
dossier = ((p.data[0].get("search_profile") if p.data else None) or {}).get("dossier") or {}
queries = (dossier.get("exa_queries") or [])[:2]
print(f"Testing {len(queries)} dossier queries\n")


def companies(r):
    out = []
    for x in getattr(r, "results", []):
        url = getattr(x, "url", "") or ""
        dom = urlparse(url).netloc.replace("www.", "") if url else ""
        if not dom or any(b in dom for b in _JUNK_DOMAINS):
            continue
        title = (getattr(x, "title", None) or dom)
        name = title.split("|")[0].split("—")[0].split("-")[0].strip()[:50]
        out.append(name)
    return out


for q in queries:
    print("=" * 80)
    print("Q:", q[:90])
    for mode in ("auto", "deep"):
        t = time.time()
        try:
            r = exa.search(q, type=mode, category="company", num_results=8)
            names = companies(r)
            print(f"\n[{mode}] {len(names)} companies in {time.time()-t:.1f}s:")
            for n in names:
                print("   -", n)
        except Exception as e:
            print(f"\n[{mode}] FAILED: {e}")
print("=" * 80)
print("DONE")
