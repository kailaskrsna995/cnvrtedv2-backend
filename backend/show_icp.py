"""Show the full ICP generation: INPUTS -> OUTPUTS (all 3 ICPs), with Exa research."""
import asyncio, logging
logging.basicConfig(level=logging.ERROR)
from app.agents.profile_agent import crawl_url_jina, generate_icp_options
from app.agents.icp_research import research_company, format_evidence

URL = "https://morphic.com/"
SERVICE = "AI creative platform to generate, animate and edit video and images for brands, agencies, studios and creators."
TARGET = "Brands, marketing teams, agencies, game/animation studios, filmmakers, and individual content creators who need high-volume video/visual content without a full production team."
OUT = []
def w(s=""): OUT.append(str(s))

async def main():
    web = await crawl_url_jina(URL, 6000)
    research = await research_company(URL, "morphic", SERVICE)
    evidence = format_evidence(research)

    w("="*70); w("INPUTS TO ICP GENERATION"); w("="*70)
    w("\n[1] WHAT THEY SELL (user input):"); w(SERVICE)
    w("\n[2] WHO THEY TARGET (user input):"); w(TARGET)
    w("\n[3] WEBSITE CRAWL (first 1200 chars):"); w((web or "EMPTY")[:1200])
    w("\n[4] EXA RESEARCH EVIDENCE (competitors/lookalikes — the 'improvement'):")
    w(evidence or "none")

    w("\n\n" + "="*70); w("OUTPUTS — 3 GENERATED ICPs"); w("="*70)
    ctx, opts, usage = await generate_icp_options(web, "", SERVICE, TARGET, research_evidence=evidence)
    w("\n--- USER CONTEXT (internal profile Sonnet built) ---"); w(ctx)
    for o in opts:
        w("\n" + "-"*60)
        w(f"ICP: {o.label}")
        w(f"SUMMARY: {o.summary}")
        w(f"FULL:\n{o.icp_text}")
    w(f"\n[cost: ${usage['cost_usd']} | tokens in {usage['input_tokens']} out {usage['output_tokens']}]")

    with open("icp_full_out.txt","w",encoding="utf-8") as f: f.write("\n".join(OUT))

asyncio.run(main())
