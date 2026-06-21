"""Validate genericized prompts + keyword field on 11fps — the dossier should still be
media-sharp (microdrama/OTT/vertical-video). If yes, genericization is safe."""
import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8")
from app.agents.profile_agent import build_seller_dossier

FPS = "f52ba30f-3de8-41ed-9f5f-abdad1de26e9"
intake = {
    "offering": "AI-native film studio — done-for-you video/film production, prompt to final frame",
    "delivery_model": "service_or_agency",
    "best_clients": ["Pocket FM", "Kuku FM", "Zee Studios", "People Media Factory"],
    "ideal_customer": "Media & entertainment companies scaling high-volume video; 50-1000 emp, Series A+; global with India strength",
    "buyer": "Head of Content / VP Marketing / Founder",
    "need_trigger": "raised funding to scale content, launching a slate, expanding to video/YouTube, hiring video/content roles",
    "price_tier": "mid-market",
    "exclude": ["traditional production houses", "mega in-house studios"],
    "dream_companies": ["Holywater Tech", "Eros Innovation", "ReelShort", "DramaBox"],
    "keywords": ["microdrama", "OTT", "vertical video", "audio-to-video", "short-form video", "regional streaming"],
}


async def main():
    d = await build_seller_dossier(FPS, intake)
    if d.get("error"):
        print("ERROR:", d["error"]); return
    print("OFFERING:", d.get("offering"))
    print("\nSEGMENTS:")
    for s in d.get("core_segments", []):
        print(f"  [{s.get('fit')}] {s.get('name')}")
    print("\nEXA QUERIES:")
    for q in d.get("exa_queries", []):
        print("  -", q)


asyncio.run(main())
