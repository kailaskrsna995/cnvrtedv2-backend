"""
COST / USAGE TRACKING
=====================
Every external API call adds to an in-memory accumulator (instant, no I/O), which
is FLUSHED into the `api_usage` table as aggregated rows — at the end of each scan
and whenever the admin dashboard loads. This avoids a DB write per Claude call
(~200/scan) while still giving accurate totals.

log_* helpers are fully error-isolated: cost tracking must NEVER break a scan.
"""

import logging
import threading
import datetime as _dt

from app.database import supabase

logger = logging.getLogger(__name__)

# ── pricing (USD per 1,000,000 tokens) ────────────────────────────────────
# Matched by substring of the model id so new dated versions still price right.
_ANTHROPIC_PRICES = [
    ("haiku",  1.0,  5.0),
    ("sonnet", 3.0, 15.0),
    ("opus",  15.0, 75.0),
]
_ANTHROPIC_DEFAULT = (3.0, 15.0)          # unknown model → assume Sonnet-tier
_OPENAI_EMBED_PER_M = 0.02                 # text-embedding-3-small
_APOLLO_PER_REVEAL = 0.05                  # estimate per revealed email (admin can correct via balance)


def _anthropic_rate(model: str):
    m = (model or "").lower()
    for key, cin, cout in _ANTHROPIC_PRICES:
        if key in m:
            return cin, cout
    return _ANTHROPIC_DEFAULT


# ── in-memory accumulator ──────────────────────────────────────────────────
# key = (provider, model) -> {"cost": float, "in": int, "out": int, "calls": int}
_acc: dict = {}
_lock = threading.Lock()


def _add(provider: str, model: str, cost: float, in_units: int = 0, out_units: int = 0):
    try:
        key = (provider, model or "")
        with _lock:
            row = _acc.setdefault(key, {"cost": 0.0, "in": 0, "out": 0, "calls": 0})
            row["cost"] += float(cost or 0)
            row["in"] += int(in_units or 0)
            row["out"] += int(out_units or 0)
            row["calls"] += 1
    except Exception:
        pass  # tracking must never raise


def log_anthropic(model: str, usage_obj):
    """usage_obj = Anthropic response.usage (has input_tokens / output_tokens)."""
    try:
        cin, cout = _anthropic_rate(model)
        it = int(getattr(usage_obj, "input_tokens", 0) or 0)
        ot = int(getattr(usage_obj, "output_tokens", 0) or 0)
        cost = it / 1_000_000 * cin + ot / 1_000_000 * cout
        _add("anthropic", model, cost, it, ot)
    except Exception:
        pass


def log_openai_embedding(model: str, total_tokens: int):
    try:
        cost = int(total_tokens or 0) / 1_000_000 * _OPENAI_EMBED_PER_M
        _add("openai", model or "embedding", cost, total_tokens or 0, 0)
    except Exception:
        pass


def log_apollo_reveal(n: int = 1):
    try:
        _add("apollo", "email_reveal", n * _APOLLO_PER_REVEAL, n, 0)
    except Exception:
        pass


def log_generic(provider: str, cost_usd: float, model: str = "", units: int = 1):
    _add(provider, model, cost_usd, units, 0)


# ── flush to DB ─────────────────────────────────────────────────────────────
def flush():
    """Write the accumulated deltas as api_usage rows, then clear the buffer.
    Safe to call often; a no-op when nothing is buffered."""
    with _lock:
        if not _acc:
            return
        snapshot = list(_acc.items())
        _acc.clear()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    rows = [{
        "provider": prov,
        "model": model,
        "input_units": v["in"],
        "output_units": v["out"],
        "cost_usd": round(v["cost"], 6),
        "meta": {"calls": v["calls"]},
        "created_at": now,
    } for (prov, model), v in snapshot]
    try:
        if supabase and rows:
            supabase.table("api_usage").insert(rows).execute()
    except Exception as e:
        logger.warning(f"[usage] flush failed (dropping {len(rows)} rows): {e}")
