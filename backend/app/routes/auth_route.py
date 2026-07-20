"""
AUTH ROUTES
===========
  POST /auth/register  → create account (email + username + password) → JWT
  POST /auth/login     → verify credentials → JWT
  GET  /auth/me        → the current logged-in user (token check)
"""

import logging

from fastapi import APIRouter, HTTPException, Depends, Request

from app.database import supabase
from app.ratelimit import limiter
from app.auth import (
    hash_password, verify_password, validate_password_strength, validate_email,
    create_access_token, get_current_user, _is_admin, dummy_password_verify,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register")
@limiter.limit("10/hour")   # cap signup spam per IP
async def register(request: Request, body: dict):
    email = validate_email(body.get("email"))
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username:
        raise HTTPException(422, "Username is required.")
    validate_password_strength(password)

    # email must be unique
    existing = supabase.table("users").select("id, password_hash").eq("email", email).limit(1).execute()
    if existing.data:
        # a legacy row may exist (synthesized during onboarding) with no password —
        # let them claim it by setting a password; otherwise it's a real duplicate.
        row = existing.data[0]
        if row.get("password_hash"):
            raise HTTPException(409, "An account with this email already exists.")
        supabase.table("users").update({
            "username": username, "password_hash": hash_password(password),
        }).eq("id", row["id"]).execute()
        user_id = row["id"]
    else:
        created = supabase.table("users").insert({
            "email": email, "username": username, "password_hash": hash_password(password),
        }).execute()
        user_id = created.data[0]["id"]

    token = create_access_token(user_id, email)
    logger.info(f"[auth] registered {email}")
    return {"token": token, "user": {"id": user_id, "email": email, "username": username,
                                     "is_admin": _is_admin(email)}}


@router.post("/login")
@limiter.limit("5/minute")   # brute-force protection per IP
async def login(request: Request, body: dict):
    email = validate_email(body.get("email"))
    password = body.get("password") or ""
    r = supabase.table("users").select("id, email, username, password_hash").eq("email", email).limit(1).execute()
    user = r.data[0] if r.data else None
    stored_hash = (user or {}).get("password_hash")
    if not stored_hash:
        # Run a dummy verify so this path costs the same time as a real check → no
        # timing-based email enumeration. Same generic error either way.
        dummy_password_verify(password)
        raise HTTPException(401, "Invalid email or password.")
    if not verify_password(password, stored_hash):
        raise HTTPException(401, "Invalid email or password.")
    token = create_access_token(user["id"], email)
    return {"token": token, "user": {"id": user["id"], "email": email,
                                     "username": user.get("username"),
                                     "is_admin": _is_admin(email)}}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return user
