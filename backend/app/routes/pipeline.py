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
  POST /pipeline/{profile_id}/move     → change a card's stage
  POST /pipeline/{profile_id}/update   → value / next_step / add a note
  POST /pipeline/{profile_id}/remove   → drop a card
Plus `on_mail_sent()` — an auto-move trigger other modules call.
"""
import datetime as _dt
import logging

from fastapi import APIRouter, Depends
from app.database import supabase
from app.auth import owned_profile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])

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


async def _move(profile_id: str, key: str, stage: str, reason: str | None = None) -> dict | None:
    """Core stage-move: update stage + append a stage_change event. Reused by the /move
    endpoint AND by auto-move triggers. Returns the updated row, or None if not found /
    bad stage. No-op (returns the row) if it's already in that stage."""
    if stage not in STAGES:
        return None
    cur = (supabase.table("pipeline_items").select("id, stage, activity")
           .eq("profile_id", profile_id).eq("lead_key", key).limit(1).execute())
    if not cur.data:
        return None
    row = cur.data[0]
    if row["stage"] == stage:
        return row
    activity = row.get("activity") or []
    label = _STAGE_LABEL.get(stage, stage)
    activity.append(_event("stage_change",
                           f"Moved to {label}" + (f" ({reason})" if reason else ""),
                           {"from": row["stage"], "to": stage}))
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
