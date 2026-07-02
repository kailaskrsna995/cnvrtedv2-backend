"""
AUTH — real per-user login + multi-tenant ownership.
=====================================================
- Passwords hashed with bcrypt (passlib). Never stored plaintext.
- Login issues a short-lived signed JWT (HS256). The frontend sends it as
  `Authorization: Bearer <token>` on every request.
- `get_current_user`  → FastAPI dependency: decodes the token, returns the caller.
- `owned_profile`     → dependency for any /{profile_id} route: 401 without a valid
                        token, 403 if the profile isn't the caller's (admins bypass).

This is what makes the app multi-tenant: a user only ever sees rows whose
profile is owned by them. Admins (founder emails) see everything.
"""

import re
import datetime as _dt

import jwt
import bcrypt
from fastapi import Depends, Header, HTTPException

from app.config import (
    JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_HOURS, ADMIN_EMAILS,
)
from app.database import supabase

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── passwords ────────────────────────────────────────────────────────────
# bcrypt hashes at most the first 72 BYTES of a password; anything longer is
# ignored by the algorithm, so we truncate to 72 bytes before hash + verify
# (must be identical on both sides). Strength is enforced separately.
def _pw_bytes(plain: str) -> bytes:
    return (plain or "").encode("utf-8")[:72]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_pw_bytes(plain), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(plain), (hashed or "").encode("utf-8"))
    except Exception:
        return False


def validate_password_strength(pw: str) -> None:
    """Strict policy: >=10 chars incl. upper, lower, number, symbol. Raises 422."""
    problems = []
    if len(pw or "") < 10:
        problems.append("at least 10 characters")
    if not re.search(r"[A-Z]", pw or ""):
        problems.append("an uppercase letter")
    if not re.search(r"[a-z]", pw or ""):
        problems.append("a lowercase letter")
    if not re.search(r"\d", pw or ""):
        problems.append("a number")
    if not re.search(r"[^A-Za-z0-9]", pw or ""):
        problems.append("a symbol")
    if problems:
        raise HTTPException(422, "Password must contain " + ", ".join(problems) + ".")


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address.")
    return email


# ── tokens ───────────────────────────────────────────────────────────────
def create_access_token(user_id: str, email: str) -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": (email or "").lower(),
        "iat": now,
        "exp": now + _dt.timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _is_admin(email: str) -> bool:
    return (email or "").lower() in ADMIN_EMAILS


# ── dependencies ─────────────────────────────────────────────────────────
async def get_current_user(authorization: str = Header(default=None)) -> dict:
    """Decode the Bearer token → {id, email, is_admin}. 401 if missing/invalid."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    token = authorization[7:].strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired — please log in again.")
    except Exception:
        raise HTTPException(401, "Invalid token")
    email = (payload.get("email") or "").lower()
    uid = payload.get("sub")
    if not uid:
        raise HTTPException(401, "Invalid token")
    return {"id": uid, "email": email, "is_admin": _is_admin(email)}


def assert_owner(profile_id: str, user: dict) -> None:
    """Raise 403 unless `user` owns `profile_id` (admins bypass)."""
    if user.get("is_admin"):
        return
    try:
        r = supabase.table("user_profiles").select("user_id").eq("id", profile_id).limit(1).execute()
    except Exception:
        # malformed id etc. — treat as not found, never leak
        raise HTTPException(404, "Profile not found")
    if not r.data:
        raise HTTPException(404, "Profile not found")
    if r.data[0].get("user_id") != user["id"]:
        raise HTTPException(403, "You don't have access to this profile.")


async def owned_profile(profile_id: str, user: dict = Depends(get_current_user)) -> dict:
    """Dependency for /{profile_id} routes: authenticates + enforces ownership.
    Returns the caller (so handlers can read user['id']/['is_admin'] if needed)."""
    assert_owner(profile_id, user)
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Dependency for /admin routes — only the founder allowlist gets through."""
    if not user.get("is_admin"):
        raise HTTPException(403, "Admins only.")
    return user
