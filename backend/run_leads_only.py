import asyncio, sys
from collections import Counter
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")
from app.routes.leads_v2 import _run_agent_and_score, _job_store

FPS = "f52ba30f-3de8-41ed-9f5f-abdad1de26e9"
METRICS = "pipeline_metrics.txt"
L = []
def log(s=""):
    print(s, flush=True); L.append(str(s))

async def main():
    log(f"=== CNVRTED METRICS (leads-only re-run) — {datetime.now():%Y-%m-%d %H:%M} ===")
    log("(reuses stored Opus dossier; tests fuzzy client-exclusion + Sonnet 4.6 judge/outreach + buyer-voice outreach)\n")
    await _run_agent_and_score(FPS)
    job = _job_store.get(FPS, {})
    log(f"status={job.get('status')}  total_signals={job.get('total_signals')}  filtered={job.get('filtered')}  passed={job.get('passed')}")
    log("\n### PER-STAGE IN/OUT ###")
    for st in (job.get("pipeline", {}) or {}).get("stages", []):
        det = "  ".join(f"{k}={v}" for k, v in (st.get("detail", {}) or {}).items())
        log(f"  [{st.get('status')}] {st.get('name'):26} {det}")
    leads = job.get("leads", []) or []
    log("\n### LEAD BREAKDOWN ###")
    log(f"  final by signal_type:   {dict(Counter(l.get('signal_type') for l in leads))}")
    log(f"  final by evidence_type: {dict(Counter(l.get('evidence_type') for l in leads))}")
    log("\n### FINAL LEADS ###")
    for l in sorted(leads, key=lambda x: x.get('intent_score', 0), reverse=True):
        log(f"[{l.get('intent_score')}] {l.get('company_name')}  ({l.get('signal_type')}/{l.get('evidence_type')})")
        log(f"   outreach: {l.get('outreach') or ''}")
    with open(METRICS, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    log(f"\n[metrics -> {METRICS}]")

asyncio.run(main())
log("DONE")
