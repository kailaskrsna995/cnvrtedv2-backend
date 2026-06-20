import asyncio, sys, json
from collections import Counter
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")
from app.agents.profile_agent import build_seller_brain
from app.routes.leads_v2 import _run_agent_and_score, _job_store

FPS = "f52ba30f-3de8-41ed-9f5f-abdad1de26e9"
METRICS = "pipeline_metrics.txt"
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
}

L = []
def log(s=""):
    print(s, flush=True); L.append(str(s))

async def main():
    log(f"=== CNVRTED PIPELINE METRICS — {datetime.now():%Y-%m-%d %H:%M} — profile 11fps ===\n")

    log("### STAGE 0: SELLER BRAIN (fresh crawl + OPUS dossier + precision targets) ###")
    brain = await build_seller_brain(FPS, intake)
    if brain.get("error"):
        log("BRAIN ERROR: " + brain["error"]); _dump(); return
    log(f"  precision_targets: {brain['precision_targets']}")
    log(f"  dream_targets:     {brain['dream_targets']}")
    log(f"  ranked segments ({len(brain['segments'])}):")
    for i, s in enumerate(brain["segments"], 1):
        log(f"    {i}. {s}")
    d = brain["dossier"]
    log(f"  anchor_companies:  {len(d.get('anchor_companies',[]))}")
    log(f"  need_signals:      {len(d.get('need_signals',[]))}")
    log(f"  exa_queries:       {len(d.get('exa_queries',[]))}")

    log("\n### FULL LEAD RUN ###")
    await _run_agent_and_score(FPS)
    job = _job_store.get(FPS, {})

    log(f"\nstatus={job.get('status')}  total_signals={job.get('total_signals')}  "
        f"filtered={job.get('filtered')}  passed={job.get('passed')}")

    log("\n### PER-STAGE INPUT/OUTPUT ###")
    for st in (job.get("pipeline", {}) or {}).get("stages", []):
        det = st.get("detail", {}) or {}
        detail = "  ".join(f"{k}={v}" for k, v in det.items())
        log(f"  [{st.get('status')}] {st.get('name'):26} {detail}")

    allc = job.get("all", []) or []
    leads = job.get("leads", []) or []

    log("\n### STAGE-WISE LEAD BREAKDOWN ###")
    log(f"  scored unique companies: {len(allc)}")
    log(f"  by signal_type (scored): {dict(Counter(c.get('signal_type') for c in allc))}")
    log(f"  by evidence_type (scored): {dict(Counter(c.get('evidence_type') for c in allc))}")
    log(f"  FINAL leads (post-judge): {len(leads)}")
    log(f"  final by signal_type:    {dict(Counter(l.get('signal_type') for l in leads))}")
    log(f"  final by evidence_type:  {dict(Counter(l.get('evidence_type') for l in leads))}")

    log("\n### FINAL LEADS ###")
    for l in sorted(leads, key=lambda x: x.get('intent_score', 0), reverse=True):
        log(f"[{l.get('intent_score')}] {l.get('company_name')}  ({l.get('signal_type')}/{l.get('evidence_type')})")
        log(f"   proof: {(l.get('proof') or '')[:130]}")
        log(f"   outreach: {l.get('outreach') or ''}")
    _dump()

def _dump():
    with open(METRICS, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    log(f"\n[metrics written -> {METRICS}]")

asyncio.run(main())
log("DONE")
