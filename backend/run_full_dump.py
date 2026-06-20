"""Run the FULL pipeline (fixes 1-3 + precision/deep) and dump EVERYTHING to a file:
pre-filter raw signals (tapped off the queue) + post-score + final leads. No log noise.
The user does the judging.
"""
import asyncio, sys
from collections import Counter
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")

import app.queue as qmod
import app.routes.leads_v2 as lv

FPS = "f52ba30f-3de8-41ed-9f5f-abdad1de26e9"
OUT = "full_results_dump.txt"

# Tap the queue: capture the exact raw signal set the real pipeline pops (pre-filter).
captured = []
_orig_pop = qmod.signal_queue.pop_batch
async def tapped(n):
    sigs = await _orig_pop(n)
    captured.extend(sigs)
    return sigs
qmod.signal_queue.pop_batch = tapped


async def main():
    await lv._run_agent_and_score(FPS)
    job = lv._job_store.get(FPS, {})

    L = []
    def w(s=""): L.append(str(s))

    w(f"=== FULL RESULTS DUMP — {datetime.now():%Y-%m-%d %H:%M} ===")
    w(f"status={job.get('status')}  total_signals={job.get('total_signals')}  "
      f"filtered={job.get('filtered')}  passed={job.get('passed')}")

    w("\n### PIPELINE STAGES (in/out per stage) ###")
    for st in (job.get("pipeline", {}) or {}).get("stages", []):
        det = "  ".join(f"{k}={v}" for k, v in (st.get("detail", {}) or {}).items())
        w(f"  [{st.get('status')}] {st.get('name'):24} {det}")

    # ---- PRE-FILTER: every raw signal harvested ----
    by = Counter(s.get("signal_type") for s in captured)
    w(f"\n\n{'='*90}\n### PRE-FILTER — ALL {len(captured)} RAW SIGNALS HARVESTED ###\nby type: {dict(by)}")
    for t in sorted(by):
        rows = [x for x in captured if x.get("signal_type") == t]
        w(f"\n----- {t.upper()} ({len(rows)}) -----")
        for s in rows:
            name = s.get("company_name") or "(no name)"
            txt = (s.get("raw_text") or "").replace("\n", " ")[:170]
            w(f"  - {name}  |  {s.get('source_platform','')}  |  {(s.get('source_url') or '')[:75]}")
            w(f"      {txt}")

    # ---- POST-SCORE: every company that survived scoring (pre-judge) ----
    allc = job.get("all") or []
    w(f"\n\n{'='*90}\n### POST-SCORE — ALL {len(allc)} SCORED COMPANIES (after scoring+dedup, pre-judge) ###")
    for r in sorted(allc, key=lambda x: x.get("intent_score", 0), reverse=True):
        w(f"  [{r.get('intent_score')}] {r.get('company_name')}  "
          f"({r.get('signal_type')}/{r.get('evidence_type')})  passed_threshold={r.get('passed')}")
        w(f"      why:   {(r.get('why') or '')[:150]}")
        w(f"      proof: {(r.get('proof') or '')[:150]}")

    # ---- FINAL: leads after judge + outreach ----
    leads = job.get("leads") or []
    w(f"\n\n{'='*90}\n### FINAL LEADS ({len(leads)}) — after judge + outreach ###")
    w(f"by signal_type:   {dict(Counter(l.get('signal_type') for l in leads))}")
    w(f"by evidence_type: {dict(Counter(l.get('evidence_type') for l in leads))}")
    for l in sorted(leads, key=lambda x: x.get("intent_score", 0), reverse=True):
        w(f"\n[{l.get('intent_score')}] {l.get('company_name')}  ({l.get('signal_type')}/{l.get('evidence_type')})")
        w(f"   proof:    {(l.get('proof') or '')[:170]}")
        w(f"   outreach: {l.get('outreach') or ''}")
        w(f"   source:   {(l.get('source_url') or '')[:90]}")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"DONE — wrote {OUT}: {len(captured)} raw signals, {len(allc)} scored, {len(leads)} final leads")


asyncio.run(main())
