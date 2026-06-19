# Box ① Profile Agent — ICP generation
Crawls the seller's website + LinkedIn, then Claude Sonnet writes UserContext + 3 ICP options.

## SYSTEM_PROMPT (verbatim constant)

```
You are a B2B sales intelligence expert specialising in agency outbound and intent-based targeting.

Your job is not to describe who a company is — it is to identify the exact moment they become ready to buy.


Rules you must follow:
1. ALWAYS trust the user's own words (what they sell, who they target) over crawled content. If they conflict, use the user's words and note the discrepancy.
2. NEVER produce generic ICPs. "B2B SaaS, 50-500 employees, Head of Marketing" is a failure. Specificity of moment is everything.
3. Trigger events must be OBSERVABLE and SEARCHABLE — something an agent can find on Reddit, a job board, a news site, or LinkedIn without calling the company.
4. The Signal-Based ICP must be defined entirely by behaviour and events — no firmographics, no industry filters. Pure intent signals only.
5. If the crawled data is thin or missing, say so clearly in the user_context and make your sharpest specific guess from what you have.
6. Extract these signals from the website if present: existing client types, case study industries, pricing tier signals (enterprise vs SMB language), repeated pain points in their copy.
7. Extract these signals from LinkedIn if present: founder's background and previous roles, team composition (hints at service capacity), types of companies that engage with their content.

8. Website content may include multiple pages labelled [HOMEPAGE], [CASE-STUD: ...], [WORK: ...] etc. Mine each page:
   - Homepage: positioning, what problem they solve
   - Case studies / work pages: actual client names, industries, outcomes — this is the most valuable signal
   - About page: team size hints, founder background
   - Services page: exact service names, pricing tier language

Output quality bar: your ICPs should read like they were written by someone who has done 10 years of B2B outbound, not a marketing textbook.
```

## generate_icp_options() — builds the user message (verbatim source)

```python
async def generate_icp_options(
    website_text: str,
    linkedin_text: str,
    service_description: str,
    target_description: str,
    research_evidence: str = "",
) -> tuple[str, list[ICPOption], dict]:
    """
    Call Claude to produce:
      - UserContext.md (what this agency sells, tone, differentiators)
      - 3 ICP options (broad / niche / signal-based)

    Returns: (user_context_text, [ICPOption, ICPOption, ICPOption])
    """
    import json

    # Flag data quality so Claude knows what to trust
    website_quality = "EMPTY — rely on user inputs only" if not website_text or len(website_text) < 200 else "OK"
    linkedin_quality = "EMPTY OR BLOCKED — do not factor in" if not linkedin_text or len(linkedin_text) < 200 else "OK"

    user_message = f"""Here is everything I know about this agency. Build their ICP.

--- USER'S OWN WORDS (highest trust) ---
What they sell: {service_description}
Who they target: {target_description}

--- WEBSITE CONTENT [quality: {website_quality}] ---
{website_text or "Not available"}

--- LINKEDIN CONTENT [quality: {linkedin_quality}] ---
{linkedin_text or "Not available"}

--- EXTERNAL RESEARCH EVIDENCE (independent of their own marketing — weight this heavily; it reflects who ACTUALLY buys) ---
{research_evidence or "Not available"}

---

Produce two things:

1. UserContext — a sharp internal profile of this agency:
   - What they actually sell (cut through any marketing fluff)
   - Pricing tier signal (enterprise, mid-market, SMB — infer from language and clients)
   - Their tone and positioning
   - Key differentiators (what they keep repeating)
   - Types of clients they've already worked with (from case studies, logos, testimonials)
   - Data quality note (flag anything missing or thin)

2. Three ICP options. Each must be meaningfully different:
   Option A — Broad ICP: wider net, more leads, lower precision. Still specific — not just an industry label.
   Option B — Niche ICP: the single tightest-fit customer type. Fewer leads, very high conversion potential.
   Option C — Signal-Based ICP: defined ONLY by observable trigger events and behaviours. No industry or size filters. Pure intent.

For each ICP include:
   - Target industry (skip for Signal-Based)
   - Company size range (skip for Signal-Based)
   - Exact buyer title (not "marketing person" — be specific)
   - The specific pain they're feeling RIGHT NOW that makes them ready to buy
   - Top 3 trigger events — each must be a real, searchable, observable moment (job posting, funding news, Reddit post, LinkedIn announcement, review site complaint, etc.)
   - One sharp one-line summary — describe the moment, not the company type

Respond in this exact JSON format with no markdown:
{{
  "user_context": "...",
  "icp_options": [
    {{
      "label": "Broad ICP",
      "summary": "...",
      "icp_text": "..."
    }},
    {{
      "label": "Niche ICP",
      "summary": "...",
      "icp_text": "..."
    }},
    {{
      "label": "Signal-Based ICP",
      "summary": "...",
      "icp_text": "..."
    }}
  ]
}}"""

    # Sonnet occasionally emits invalid JSON in the long prose fields (unescaped
    # quotes/commas). Retry a few times — it's stochastic, regeneration fixes it.
    last_err = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": "{"},  # prefill: force pure JSON, no preamble
                ],
            )

            raw = "{" + response.content[0].text  # re-add prefilled brace
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            data = _loads_tolerant(raw)
            user_context = data.get("user_context", "")
            icp_options = [ICPOption(**opt) for opt in data.get("icp_options", [])]
            if not icp_options:
                raise ValueError("no icp_options in response")

            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost_usd = (input_tokens / 1_000_000 * 3.00) + (output_tokens / 1_000_000 * 15.00)
            logger.info(f"[ProfileAgent] tokens in={input_tokens} out={output_tokens} cost=${cost_usd:.5f} (attempt {attempt+1})")
            return user_context, icp_options, {"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": round(cost_usd, 5)}

        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            logger.warning(f"[ProfileAgent] JSON parse failed (attempt {attempt+1}/3): {e}")
            continue
        except Exception as e:
            logger.error(f"[ProfileAgent] generate_icp_options failed: {e}")
            raise

    logger.error(f"[ProfileAgent] All 3 attempts returned malformed JSON: {last_err}")
    raise ValueError("ICP generation failed — please try again.")

```
