"""
OUTREACH GENERATOR
==================
Writes one personalised outreach line per lead.
Max 20 words. In the agency's voice. Ready to paste.

Uses Claude Sonnet (needs quality, not speed).

What it reads:
  - UserContext.md (the agency's tone + what they sell)
  - Signal type (funding / hiring / buyer post / news)
  - Decision maker name + title
  - Why the company was flagged

Example output:
  "Congrats on the Series A Sarah — we help funded SaaS teams
   turn new budget into pipeline fast."
"""

import logging
from app.llm import Anthropic
from app.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)
client = Anthropic(api_key=ANTHROPIC_API_KEY)

SONNET_MODEL = "claude-sonnet-4-5"

OUTREACH_PROMPT = """Write ONE personalised outreach opening line for this lead.

Agency context (their voice + what they sell):
{user_context}

Signal:
Type: {signal_type}
Why flagged: {why_flagged}

Decision maker: {decision_maker_name}, {decision_maker_title}

Rules:
- Maximum 20 words
- Sound human, not salesy
- Reference the specific signal (funding raise, hiring gap, etc.)
- Write in the agency's voice
- Do NOT include a call to action
- Output ONLY the line, nothing else"""


async def generate_outreach_line(
    user_context: str,
    signal_type: str,
    why_flagged: str,
    decision_maker_name: str = "",
    decision_maker_title: str = "",
) -> str:
    """Generate one outreach line. Returns empty string on failure."""
    name  = decision_maker_name or "there"
    title = decision_maker_title or ""

    prompt = OUTREACH_PROMPT.format(
        user_context=user_context[:1500],
        signal_type=signal_type,
        why_flagged=why_flagged[:300],
        decision_maker_name=name,
        decision_maker_title=title,
    )

    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        line = response.content[0].text.strip().strip('"')
        # Enforce 20 word limit
        words = line.split()
        if len(words) > 20:
            line = " ".join(words[:20])
        return line
    except Exception as e:
        logger.error(f"[Outreach] Generation failed: {e}")
        return ""
