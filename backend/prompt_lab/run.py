"""
ICP PROMPT LAB — experiment with ICP-generation prompts efficiently.

Idea: ICP generation is ONE Sonnet call. The slow part is the website crawl, so we
crawl ONCE, cache the inputs, then fire any number of candidate prompts against the
SAME inputs and write comparable outputs. No DB writes, no re-crawl, no full pipeline.

USAGE (from backend/):
  .\venv\Scripts\python.exe prompt_lab\run.py                # run all prompts in prompt_lab/prompts/
  .\venv\Scripts\python.exe prompt_lab\run.py --facets       # also show resulting search facets
  .\venv\Scripts\python.exe prompt_lab\run.py --refresh      # re-crawl (ignore cached inputs)

TO TEST THE VC'S NEW PROMPT:
  1. Save it as a .txt file in prompt_lab/prompts/  (e.g. vc_v1.txt)
  2. Run the command above
  3. Compare prompt_lab/outputs/*.md  (00_current.md is the baseline)
"""
import os, sys, json, asyncio, argparse
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # backend/ on path so `app` imports

from app.database import supabase
from app.agents import profile_agent

PROFILE = os.environ.get("LAB_PROFILE", "f52ba30f-3de8-41ed-9f5f-abdad1de26e9")  # 11fps
PROMPTS_DIR = os.path.join(HERE, "prompts")
OUT_DIR = os.path.join(HERE, "outputs")
INPUTS_CACHE = os.path.join(HERE, f"_inputs_{PROFILE}.json")
os.makedirs(PROMPTS_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)


async def get_inputs(refresh=False) -> dict:
    """Crawl website+LinkedIn ONCE and cache. Reuse on every later run."""
    if not refresh and os.path.exists(INPUTS_CACHE):
        with open(INPUTS_CACHE, encoding="utf-8") as f:
            print(f"[lab] using cached inputs ({INPUTS_CACHE})")
            return json.load(f)
    p = supabase.table("user_profiles").select(
        "website_url, linkedin_url, service_description, target_description").eq("id", PROFILE).execute().data[0]
    print(f"[lab] crawling {p.get('website_url')} (one-time)...")
    website = await profile_agent.deep_crawl_website(p.get("website_url", ""))
    linkedin = await profile_agent.crawl_url(p.get("linkedin_url", ""), char_limit=6000)
    inputs = {"website_text": website, "linkedin_text": linkedin,
              "service_description": p.get("service_description", "") or "",
              "target_description": p.get("target_description", "") or ""}
    with open(INPUTS_CACHE, "w", encoding="utf-8") as f:
        json.dump(inputs, f, indent=2)
    print(f"[lab] cached inputs: website={len(website)} chars, linkedin={len(linkedin)} chars")
    return inputs


def ensure_baseline():
    """Seed 00_current.txt with the LIVE SYSTEM_PROMPT if prompts/ is empty."""
    if not any(f.endswith(".txt") for f in os.listdir(PROMPTS_DIR)):
        with open(os.path.join(PROMPTS_DIR, "00_current.txt"), "w", encoding="utf-8") as f:
            f.write(profile_agent.SYSTEM_PROMPT)
        print("[lab] seeded prompts/00_current.txt with the live SYSTEM_PROMPT")


async def run_one(name: str, system_prompt: str, inputs: dict, with_facets: bool) -> str:
    uc, opts, meta = await profile_agent.generate_icp_options(
        inputs["website_text"], inputs["linkedin_text"],
        inputs["service_description"], inputs["target_description"],
        system_prompt=system_prompt)
    lines = [f"# PROMPT: {name}", f"_cost ${meta.get('cost_usd')} · in {meta.get('input_tokens')} / out {meta.get('output_tokens')} tokens_\n",
             "## UserContext\n", uc[:1500], "\n## ICP OPTIONS\n"]
    for o in opts:
        lines.append(f"### {o.label}\n**{o.summary}**\n\n{o.icp_text}\n")
    if with_facets:
        # the Broad ICP's resulting search facets — the real downstream effect
        broad = next((o for o in opts if "broad" in o.label.lower()), opts[0])
        sp = await profile_agent.build_search_profile(broad.icp_text, uc)
        lines.append("## SEARCH FACETS (from Broad ICP)\n```json\n" + json.dumps(sp, indent=2) + "\n```")
    out = "\n".join(lines)
    with open(os.path.join(OUT_DIR, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write(out)
    return f"{name}: {len(opts)} ICPs, ${meta.get('cost_usd')}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--facets", action="store_true", help="also compute resulting search facets")
    ap.add_argument("--refresh", action="store_true", help="re-crawl inputs")
    args = ap.parse_args()

    ensure_baseline()
    inputs = await get_inputs(refresh=args.refresh)
    prompt_files = sorted(f for f in os.listdir(PROMPTS_DIR) if f.endswith(".txt"))
    print(f"[lab] running {len(prompt_files)} prompt(s) against profile {PROFILE}\n")
    for pf in prompt_files:
        name = pf[:-4]
        with open(os.path.join(PROMPTS_DIR, pf), encoding="utf-8") as f:
            sp = f.read()
        try:
            print("  ✓", await run_one(name, sp, inputs, args.facets))
        except Exception as e:
            print(f"  ✗ {name} FAILED: {e}")
    print(f"\n[lab] outputs in prompt_lab/outputs/ — compare *.md (00_current is the baseline)")

asyncio.run(main())
