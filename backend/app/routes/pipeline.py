"""
PIPELINE ROUTES — the TRACK backbone.
====================================
Each row in `pipeline_items` (migration 005) is one deal card — the durable record of a
lead as it moves through the sales stages. This is how cnvrted REMEMBERS a deal (Postgres,
not the ephemeral in-memory scan store). Every write appends a TYPED entry to the card's
`activity` log — the single source of truth for the deal's history that the co-pilot (and
later the canvas / conversational-intelligence) reads.

Endpoints (all ownership-gated):
  GET  /pipeline/{profile_id}          → the board (all cards for the profile)
  POST /pipeline/{profile_id}/add      → drop a lead into the pipeline (idempotent)
  POST /pipeline/{profile_id}/move           → change a card's stage
  POST /pipeline/{profile_id}/analyze-reply  → AI reads a pasted reply → suggests stage (read-only)
  POST /pipeline/{profile_id}/mark-replied   → commit the reply → advance stage + log a reply event
  POST /pipeline/{profile_id}/update         → value / next_step / add a note
  POST /pipeline/{profile_id}/remove   → drop a card
Plus `on_mail_sent()` — an auto-move trigger other modules call.
"""
import datetime as _dt
import json
import logging

from fastapi import APIRouter, Depends
from app.database import supabase
from app.auth import owned_profile
from app.config import ANTHROPIC_API_KEY
from app.llm import AsyncAnthropic

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])

# Haiku reads a pasted reply → suggests the deal's next stage (used by /analyze-reply).
# Cheap, fast, and cost-tracked via app.llm. Read-only — nothing is written until the
# seller confirms and /mark-replied commits it.
_HAIKU = "claude-haiku-4-5-20251001"
_claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
_CLASSIFY_SYSTEM = (
    "You are a sales co-pilot. A seller received a reply from a prospect in their pipeline. "
    "Read the reply and decide the deal's new stage, a one-line summary, and the sentiment.\n"
    "Stages:\n"
    "- replied: responded/acknowledged but no clear next step.\n"
    "- meeting: they want a call, demo, or to meet.\n"
    "- in_talks: actively discussing details, pricing, or negotiating.\n"
    "- won: they agreed to buy / move forward.\n"
    "- lost: they declined or aren't interested.\n"
    'Respond with ONLY a JSON object: {"stage": <one of the stages>, '
    '"summary": <one short line>, "sentiment": "positive"|"neutral"|"negative", '
    '"reasoning": <short>}'
)

# The stages, in order. Stored as text on the row so renaming/reordering later is trivial.
STAGES = ["new", "contacted", "replied", "meeting", "in_talks", "won", "lost"]
_STAGE_LABEL = {"new": "New", "contacted": "Contacted", "replied": "Replied",
                "meeting": "Meeting", "in_talks": "In Talks", "won": "Won", "lost": "Lost"}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _event(etype: str, text: str, meta: dict | None = None) -> dict:
    """One typed entry in a card's activity log."""
    e = {"type": etype, "text": text, "at": _now()}
    if meta:
        e["meta"] = meta
    return e


def _norm_key(lead_key: str, lead: dict | None = None) -> str:
    """Stable per-lead id, normalized. Prefer an explicit lead_key (what the frontend uses
    for the mail flow too, so auto-move matches); else derive from the lead."""
    key = (lead_key or "").strip().lower()
    if key:
        return key
    l = lead or {}
    return (l.get("company_name") or l.get("company") or l.get("source_url") or "").strip().lower()


async def _classify_reply(content: str, company: str | None = None) -> dict:
    """Read a prospect's reply → SUGGEST {stage, summary, sentiment, reasoning}. Read-only
    (no DB write). Robust: falls back to a safe default if the model or its JSON misbehaves,
    so a flaky LLM never blocks the seller from logging a reply."""
    fallback = {"stage": "replied", "summary": "Reply received", "sentiment": "neutral", "reasoning": ""}
    text = (content or "").strip()
    if not text:
        return fallback
    ctx = f"Company: {company}\n" if company else ""
    try:
        resp = await _claude.messages.create(
            model=_HAIKU,
            max_tokens=220,
            system=_CLASSIFY_SYSTEM,
            messages=[
                {"role": "user", "content": f'{ctx}Prospect reply:\n"""\n{text}\n"""'},
                {"role": "assistant", "content": "{"},  # prefill → forces clean JSON
            ],
        )
        raw = "{" + (resp.content[0].text if resp.content else "")
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        stg = str(data.get("stage", "")).strip().lower()
        if stg not in STAGES:
            stg = "replied"
        return {
            "stage": stg,
            "summary": (str(data.get("summary") or "").strip() or "Reply received")[:200],
            "sentiment": str(data.get("sentiment") or "neutral").strip().lower(),
            "reasoning": str(data.get("reasoning") or "").strip()[:200],
        }
    except Exception as e:
        logger.warning(f"[pipeline] reply classify failed: {e}")
        return fallback


@router.get("/{profile_id}")
async def list_pipeline(profile_id: str, user: dict = Depends(owned_profile)):
    try:
        rows = (supabase.table("pipeline_items")
                .select("*").eq("profile_id", profile_id)
                .order("updated_at", desc=True).execute()).data or []
    except Exception as e:
        logger.warning(f"[pipeline] list failed: {e}")
        rows = []
    return {"items": rows, "stages": STAGES}


@router.post("/{profile_id}/add")
async def add_to_pipeline(profile_id: str, body: dict, user: dict = Depends(owned_profile)):
    lead = body.get("lead") or {}
    key = _norm_key(body.get("lead_key"), lead)
    if not key:
        return {"status": "error", "message": "missing lead"}
    company = body.get("company") or lead.get("company_name") or lead.get("company")
    # Idempotent: if a card already exists for this lead, don't duplicate or reset it.
    existing = (supabase.table("pipeline_items").select("id")
                .eq("profile_id", profile_id).eq("lead_key", key).limit(1).execute())
    if existing.data:
        return {"status": "exists", "id": existing.data[0]["id"]}
    row = {
        "profile_id": profile_id,
        "lead_key": key,
        "company": company,
        "lead": lead,
        "stage": "new",
        "activity": [_event("created", f"Added {company or 'lead'} to pipeline")],
    }
    try:
        res = supabase.table("pipeline_items").insert(row).execute()
        return {"status": "added", "item": (res.data or [None])[0]}
    except Exception as e:
        logger.warning(f"[pipeline] add failed: {e}")
        return {"status": "error"}


async def _move(profile_id: str, key: str, stage: str, reason: str | None = None,
                event_type: str = "stage_change", event_text: str | None = None,
                event_meta: dict | None = None, force_event: bool = False) -> dict | None:
    """Core stage-move: update stage + append an activity event. Reused by the /move
    endpoint, the mark-replied action, AND by auto-move triggers.
      - `event_type`/`event_text`/`event_meta` let a caller log a richer SEMANTIC event
        (e.g. a first-class 'reply' carrying the reply text + sentiment) instead of the
        generic stage_change, so the timeline, Orka, and later the automation/canvas can
        key off WHAT happened, not just that a stage changed.
      - `force_event=True` logs the event even when the stage is unchanged (a prospect can
        reply again while already 'In Talks' — we still want that reply on the record).
    Returns the updated row, or None if not found / bad stage. No-op (returns the row) if
    it's already in that stage AND force_event is False."""
    if stage not in STAGES:
        return None
    cur = (supabase.table("pipeline_items").select("id, stage, activity")
           .eq("profile_id", profile_id).eq("lead_key", key).limit(1).execute())
    if not cur.data:
        return None
    row = cur.data[0]
    if row["stage"] == stage and not force_event:
        return row
    activity = row.get("activity") or []
    label = _STAGE_LABEL.get(stage, stage)
    text = event_text or ("Moved to " + label + (f" ({reason})" if reason else ""))
    meta = {"from": row["stage"], "to": stage}
    if event_meta:
        meta.update(event_meta)
    activity.append(_event(event_type, text, meta))
    upd = (supabase.table("pipeline_items")
           .update({"stage": stage, "activity": activity, "updated_at": _now()})
           .eq("id", row["id"]).execute())
    return (upd.data or [None])[0]


@router.post("/{profile_id}/move")
async def move_stage(profile_id: str, body: dict, user: dict = Depends(owned_profile)):
    key = _norm_key(body.get("lead_key"))
    stage = (body.get("stage") or "").strip().lower()
    moved = await _move(profile_id, key, stage)
    if moved is None:
        return {"status": "error", "message": "not found or invalid stage"}
    return {"status": "moved", "item": moved}


@router.post("/{profile_id}/analyze-reply")
async def analyze_reply(profile_id: str, body: dict, user: dict = Depends(owned_profile)):
    """Read-only: read a pasted reply and SUGGEST {stage, summary, sentiment}. Writes
    nothing — the seller confirms (or overrides) the suggestion, then /mark-replied commits
    it. Split from the commit so the AI read never mutates the deal on its own."""
    company = None
    key = _norm_key(body.get("lead_key"))
    if key:
        cur = (supabase.table("pipeline_items").select("company")
               .eq("profile_id", profile_id).eq("lead_key", key).limit(1).execute())
        if cur.data:
            company = cur.data[0].get("company")
    suggestion = await _classify_reply(body.get("content") or "", company)
    return {"status": "ok", "suggestion": suggestion}


@router.post("/{profile_id}/mark-replied")
async def mark_replied(profile_id: str, body: dict, user: dict = Depends(owned_profile)):
    """Commit a reply: advance the deal to the (confirmed) `stage` and log a first-class
    `reply` event carrying the reply text + sentiment. `stage`/`summary`/`sentiment` come
    from the seller confirming the AI suggestion; all optional → falls back to a plain
    'Replied'. Manual stand-in for inbox-sync reply tracking (Phase 2): this event is the
    seam a follow-up sequence will later key off to stop chasing + trigger next-best-action."""
    key = _norm_key(body.get("lead_key"))
    stage = (body.get("stage") or "replied").strip().lower()
    if stage not in STAGES:
        stage = "replied"
    content = (body.get("content") or "").strip()
    summary = (body.get("summary") or "").strip()
    sentiment = (body.get("sentiment") or "").strip().lower()
    meta: dict = {}
    if content:
        meta["content"] = content
    if sentiment:
        meta["sentiment"] = sentiment
    moved = await _move(profile_id, key, stage, event_type="reply",
                        event_text=summary or "Reply received",
                        event_meta=meta or None, force_event=True)
    if moved is None:
        return {"status": "error", "message": "not found"}
    return {"status": "replied", "item": moved}


@router.post("/{profile_id}/update")
async def update_item(profile_id: str, body: dict, user: dict = Depends(owned_profile)):
    key = _norm_key(body.get("lead_key"))
    cur = (supabase.table("pipeline_items").select("id, activity")
           .eq("profile_id", profile_id).eq("lead_key", key).limit(1).execute())
    if not cur.data:
        return {"status": "error", "message": "not found"}
    row = cur.data[0]
    patch: dict = {"updated_at": _now()}
    if "value" in body:
        patch["value"] = body.get("value")
    if "next_step" in body:
        patch["next_step"] = body.get("next_step")
    note = (body.get("note") or "").strip()
    if note:
        activity = row.get("activity") or []
        activity.append(_event("note", note))
        patch["activity"] = activity
    try:
        res = supabase.table("pipeline_items").update(patch).eq("id", row["id"]).execute()
        return {"status": "updated", "item": (res.data or [None])[0]}
    except Exception as e:
        logger.warning(f"[pipeline] update failed: {e}")
        return {"status": "error"}


@router.post("/{profile_id}/remove")
async def remove_item(profile_id: str, body: dict, user: dict = Depends(owned_profile)):
    key = _norm_key(body.get("lead_key"))
    try:
        (supabase.table("pipeline_items").delete()
         .eq("profile_id", profile_id).eq("lead_key", key).execute())
    except Exception as e:
        logger.warning(f"[pipeline] remove failed: {e}")
    return {"status": "removed"}


# ── auto-move triggers (called by other modules, not HTTP) ─────────────────
async def on_mail_sent(profile_id: str, lead_key: str) -> None:
    """Marking a mail sent puts the deal in Contacted — AUTO-POPULATE: create the card if it
    isn't in the pipeline yet (using the mail_items lead snapshot), else advance it from New.
    So the pipeline builds itself as the seller does outreach. Best-effort, never raises."""
    try:
        raw = (lead_key or "").strip()          # mail_items stores the key as-sent (not normalized)
        key = _norm_key(raw)                     # pipeline keys are normalized
        if not key:
            return
        cur = (supabase.table("pipeline_items").select("id, stage")
               .eq("profile_id", profile_id).eq("lead_key", key).limit(1).execute())
        if cur.data:
            if cur.data[0]["stage"] == "new":
                await _move(profile_id, key, "contacted", reason="email sent")
            return
        # Not tracked yet → create the card in Contacted from the mail's stored lead snapshot.
        mi = (supabase.table("mail_items").select("company, lead")
              .eq("profile_id", profile_id).eq("lead_key", raw).limit(1).execute())
        company = mi.data[0].get("company") if mi.data else None
        lead = (mi.data[0].get("lead") if mi.data else None) or {}
        supabase.table("pipeline_items").insert({
            "profile_id": profile_id,
            "lead_key": key,
            "company": company,
            "lead": lead,
            "stage": "contacted",
            "activity": [
                _event("created", f"Added {company or 'lead'} to pipeline (email sent)"),
                _event("stage_change", "Moved to Contacted (email sent)", {"from": "new", "to": "contacted"}),
            ],
        }).execute()
    except Exception as e:
        logger.debug(f"[pipeline] on_mail_sent skipped: {e}")
