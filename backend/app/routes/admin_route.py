"""
ADMIN ROUTES  (founder allowlist only — every route gated by require_admin)
  GET  /admin/summary   → spend totals, by-provider, by-day, + usage stats
  GET  /admin/balances  → per-provider balance / estimated remaining / monthly
  POST /admin/balances  → set a provider's balance (top-up) or monthly cost
"""

import datetime as _dt
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.database import supabase
from app.auth import require_admin
from app import usage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_USAGE_FETCH_LIMIT = 20000  # aggregated rows are low-volume; plenty of headroom


def _parse(ts: str) -> _dt.datetime:
    try:
        return _dt.datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except Exception:
        return _dt.datetime.now(_dt.timezone.utc)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _count(table: str, **eq) -> int:
    try:
        q = supabase.table(table).select("id", count="exact")
        for k, v in eq.items():
            q = q.eq(k, v)
        return q.execute().count or 0
    except Exception:
        return 0


@router.get("/summary")
async def summary(_admin: dict = Depends(require_admin)):
    usage.flush()  # make sure the latest in-memory spend is persisted first
    now = _now()
    d1 = now - _dt.timedelta(days=1)
    d7 = now - _dt.timedelta(days=7)
    d30 = now - _dt.timedelta(days=30)

    try:
        rows = (supabase.table("api_usage")
                .select("provider, cost_usd, created_at")
                .order("created_at", desc=True)
                .limit(_USAGE_FETCH_LIMIT).execute().data) or []
    except Exception as e:
        logger.warning(f"[admin] usage fetch failed: {e}")
        rows = []

    total_all = total_1 = total_7 = total_30 = 0.0
    by_provider: dict = {}
    by_day: dict = {}
    for r in rows:
        c = float(r.get("cost_usd") or 0)
        ts = _parse(r.get("created_at"))
        total_all += c
        if ts >= d1: total_1 += c
        if ts >= d7: total_7 += c
        if ts >= d30:
            total_30 += c
            day = ts.date().isoformat()
            by_day[day] = by_day.get(day, 0.0) + c
        prov = r.get("provider") or "unknown"
        by_provider[prov] = by_provider.get(prov, 0.0) + c

    rnd = lambda x: round(x, 4)
    return {
        "spend": {
            "today": rnd(total_1), "last_7d": rnd(total_7),
            "last_30d": rnd(total_30), "all_time": rnd(total_all),
        },
        "by_provider": {k: rnd(v) for k, v in sorted(by_provider.items(), key=lambda x: -x[1])},
        "by_day": [{"date": d, "cost": rnd(by_day[d])} for d in sorted(by_day)],
        "stats": {
            "users": _count("users"),
            "profiles": _count("user_profiles"),
            "scans_total": _count("scan_runs"),
        },
    }


@router.get("/users")
async def users_overview(_admin: dict = Depends(require_admin)):
    """Per-user activity for the admin dashboard — email, username, # profiles,
    # scans (searches), first/last scan. Read-only join over users + scan_runs +
    user_profiles. Sorted most-active first."""
    from app.config import ADMIN_EMAILS

    try:
        users = supabase.table("users").select("*").execute().data or []
    except Exception as e:
        logger.warning(f"[admin] users fetch failed: {e}")
        users = []
    try:
        scans = (supabase.table("scan_runs")
                 .select("user_id, profile_id, created_at")
                 .order("created_at", desc=True)
                 .limit(_USAGE_FETCH_LIMIT).execute().data) or []
    except Exception as e:
        logger.warning(f"[admin] scan_runs fetch failed: {e}")
        scans = []
    try:
        profiles = supabase.table("user_profiles").select("id, name, user_id").execute().data or []
    except Exception:
        profiles = []

    profile_name = {p["id"]: (p.get("name") or "—") for p in profiles}
    profiles_by_user: dict = {}
    for p in profiles:
        profiles_by_user.setdefault(p.get("user_id"), []).append(p.get("name") or "—")

    # scan_runs come newest-first → index 0 is the most recent scan for each user
    scans_by_user: dict = {}
    for s in scans:
        scans_by_user.setdefault(s.get("user_id"), []).append(s)

    out = []
    for u in users:
        uid = u.get("id")
        email = u.get("email") or ""
        u_scans = scans_by_user.get(uid, [])
        recent = [{"profile": profile_name.get(s.get("profile_id"), "—"),
                   "at": s.get("created_at")} for s in u_scans[:10]]
        out.append({
            "id": uid,
            "email": email,
            "username": u.get("username") or "—",
            "is_admin": email.lower() in ADMIN_EMAILS,
            "joined": u.get("created_at"),
            "profiles": len(profiles_by_user.get(uid, [])),
            "profile_names": profiles_by_user.get(uid, []),
            "scans": len(u_scans),
            "last_scan": u_scans[0]["created_at"] if u_scans else None,
            "first_scan": u_scans[-1]["created_at"] if u_scans else None,
            "recent_scans": recent,
        })

    out.sort(key=lambda x: x["scans"], reverse=True)   # most active first
    return {
        "users": out,
        "totals": {
            "users": len(users),
            "scans": len(scans),
            "active_users": sum(1 for u in out if u["scans"] > 0),
        },
    }


@router.get("/balances")
async def get_balances(_admin: dict = Depends(require_admin)):
    usage.flush()
    try:
        accounts = supabase.table("provider_balances").select("*").execute().data or []
    except Exception:
        accounts = []
    try:
        usage_rows = (supabase.table("api_usage")
                      .select("provider, cost_usd, created_at")
                      .order("created_at", desc=True).limit(_USAGE_FETCH_LIMIT).execute().data) or []
    except Exception:
        usage_rows = []

    out = []
    for a in accounts:
        prov = a.get("provider")
        rnd = lambda x: round(float(x or 0), 2)
        if a.get("is_fixed"):
            out.append({"provider": prov, "is_fixed": True,
                        "monthly_usd": rnd(a.get("monthly_usd")), "note": a.get("note")})
        else:
            since = _parse(a.get("balance_set_at"))
            spent = sum(float(r.get("cost_usd") or 0) for r in usage_rows
                        if r.get("provider") == prov and _parse(r.get("created_at")) >= since)
            bal = float(a.get("balance_usd") or 0)
            out.append({
                "provider": prov, "is_fixed": False,
                "balance_usd": rnd(bal), "spent_since": round(spent, 4),
                "remaining_est": round(bal - spent, 2),
                "balance_set_at": a.get("balance_set_at"), "note": a.get("note"),
            })
    out.sort(key=lambda x: (x["is_fixed"], x["provider"]))
    return {"providers": out}


@router.post("/balances")
async def set_balance(body: dict, _admin: dict = Depends(require_admin)):
    """Body: { provider, is_fixed?, balance_usd?, monthly_usd?, note? }.
    Setting balance_usd resets the 'spent since' clock (a top-up / correction)."""
    provider = (body.get("provider") or "").strip().lower()
    if not provider:
        raise HTTPException(422, "provider is required")
    row: dict = {"provider": provider, "updated_at": _now().isoformat()}
    if "is_fixed" in body:      row["is_fixed"] = bool(body["is_fixed"])
    if "monthly_usd" in body:   row["monthly_usd"] = float(body["monthly_usd"] or 0)
    if "note" in body:          row["note"] = body["note"]
    if "balance_usd" in body:
        row["balance_usd"] = float(body["balance_usd"] or 0)
        row["balance_set_at"] = _now().isoformat()   # new top-up → reset spend clock
    try:
        supabase.table("provider_balances").upsert(row, on_conflict="provider").execute()
    except Exception as e:
        raise HTTPException(500, f"Save failed: {e}")
    return {"status": "saved", "provider": provider}
