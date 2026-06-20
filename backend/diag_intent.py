"""Diagnose the buyer-intent funnel for 11fps: harvest intent signals, score each,
print exactly WHERE and WHY they die (is_lead / evidence_type / modality / score / judge).
"""
import asyncio, sys
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
from app.database import supabase
from app.queue import signal_queue
from app.agents.buyer_intent_agent import run as run_buyer
from app.pipeline.scoring import score_signal, judge_leads
from app.config import INTENT_SCORE_THRESHOLD

FPS = "f52ba30f-3de8-41ed-9f5f-abdad1de26e9"


async def main():
    p = supabase.table("user_profiles").select("icp_text,user_context,search_profile") \
        .eq("id", FPS).execute().data[0]
    sp = p.get("search_profile") or {}
    dossier = sp.get("dossier")
    dm = sp.get("seller_delivery_model")
    print(f"delivery_model={dm}  threshold={INTENT_SCORE_THRESHOLD}")
    print(f"buyer_language used: {(dossier or {}).get('buyer_language', [])[:6]}\n")

    stats = await run_buyer(FPS)
    print(f"buyer agent stats: harvested={stats.get('harvested')} "
          f"buyer_language={stats.get('buyer_language')} queued={stats.get('signals_queued')}\n")

    sigs = await signal_queue.pop_batch(200)
    bi = [s for s in sigs if s.get("signal_type") == "buyer_intent"]
    print(f"pulled {len(bi)} buyer_intent signals from queue\n" + "=" * 100)

    async def sc(s):
        r = await score_signal(s.get("raw_text", ""), "buyer_intent",
                               p["user_context"], p["icp_text"], dm, dossier)
        return s, r

    scored = await asyncio.gather(*[sc(s) for s in bi])

    kept_for_judge = []
    by_lead, by_ev, by_band = Counter(), Counter(), Counter()
    for s, r in scored:
        is_lead = r.get("is_lead", True)
        ev = r.get("evidence_type", "?")
        score = r.get("score", 0)
        by_lead[is_lead] += 1
        if is_lead is not False:
            by_ev[ev] += 1
            band = "≥.62" if score >= 0.62 else (".40-.62" if score >= 0.4 else "<.40")
            by_band[band] += 1
        txt = (s.get("raw_text", "") or "").replace("\n", " ")[:130]
        flag = "CUT(is_lead)" if is_lead is False else ("PASS" if score >= 0.62 else "below-thr")
        print(f"[{score:.2f}] {ev:13} {flag:12} | {txt}")
        print(f"        why: {(r.get('why') or '')[:110]}")
        if is_lead is not False and score >= 0.62:
            kept_for_judge.append({**s, **r, "company_name": s.get("company_name") or r.get("company_name", "")})

    print("=" * 100)
    print(f"is_lead breakdown:     {dict(by_lead)}")
    print(f"evidence_type (kept):  {dict(by_ev)}")
    print(f"score bands (kept):    {dict(by_band)}")
    print(f"passed threshold 0.62: {len(kept_for_judge)}")

    if kept_for_judge:
        verdict = await judge_leads(kept_for_judge, p["user_context"], p["icp_text"], dm, dossier)
        print(f"after judge:           {len(verdict['keep'])} kept, "
              f"{len(kept_for_judge) - len(verdict['keep'])} cut")
        for l in verdict["keep"]:
            print(f"   KEEP [{l.get('score'):.2f}] {l.get('company_name')} [{l.get('evidence_type')}]")
    print("DONE")


asyncio.run(main())
