"""
PROFILES ROUTES
===============
Handles everything profile-related:
  POST /profiles/              → create new profile, trigger Profile Agent
  GET  /profiles/{user_id}     → list all profiles for a user
  GET  /profiles/{id}/icp      → get ICP options (3 suggestions)
  POST /profiles/{id}/approve  → user approves ICP
  POST /profiles/{id}/chat     → ICP building chatbot message
  PUT  /profiles/{id}          → update profile
  DELETE /profiles/{id}        → delete profile
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
import logging
logger = logging.getLogger(__name__)
from app.models import ProfileCreate, ICPApproval, ICPChatMessage
from app.database import supabase
from app.agents import profile_agent
from app.auth import get_current_user, owned_profile
from app.config import MAX_PROFILES_PER_USER

router = APIRouter(prefix="/profiles", tags=["profiles"])


def _assert_profile_quota(user: dict):
    """Non-admins can own at most MAX_PROFILES_PER_USER profiles."""
    if user.get("is_admin"):
        return
    try:
        r = supabase.table("user_profiles").select("id", count="exact") \
            .eq("user_id", user["id"]).execute()
        count = r.count or 0
    except Exception:
        return  # never block creation on a count error
    if count >= MAX_PROFILES_PER_USER:
        raise HTTPException(
            403,
            f"Trial limit reached — up to {MAX_PROFILES_PER_USER} profiles. "
            f"Delete one or reach out to unlock more.",
        )


@router.post("/save-icp")
async def save_icp_to_db(body: dict, user: dict = Depends(get_current_user)):
    """
    Save chosen ICP to DB and generate vector.
    Called when user clicks 'Use this' on onboarding.
    The profile is owned by the logged-in user.
    """
    try:
        website_url = body.get("website_url", "")
        linkedin_url = body.get("linkedin_url", "")
        service_description = body.get("service_description", "")
        target_description = body.get("target_description", "")
        chosen_icp_text = body.get("chosen_icp_text", "")
        user_context = body.get("user_context", "")
        _assert_profile_quota(user)

        # 1. Owned by the authenticated user
        user_id = user["id"]

        # 2. Generate ICP vector + search facets
        from app.pipeline.matching import vectorise_text
        icp_vector = await vectorise_text(chosen_icp_text)
        search_profile = await profile_agent.build_search_profile(chosen_icp_text, user_context)

        # 3. Create profile
        profile_result = supabase.table("user_profiles").insert({
            "user_id": user_id,
            "name": body.get("name", "My Profile"),
            "website_url": website_url,
            "linkedin_url": linkedin_url,
            "service_description": service_description,
            "target_description": target_description,
            "user_context": user_context,
            "icp_text": chosen_icp_text,
            "icp_vector": icp_vector,
            "search_profile": search_profile,
        }).execute()

        profile_id = profile_result.data[0]["id"]
        logger.info(f"[save-icp] Profile saved: {profile_id}")

        # Store competitors (so they're shown separately + excluded from leads)
        try:
            from app.agents.icp_research import populate_competitors
            await populate_competitors(profile_id, website_url, service_description)
        except Exception as e:
            logger.warning(f"[save-icp] competitor populate failed: {e}")

        return {"status": "saved", "profile_id": profile_id, "user_id": user_id}

    except Exception as e:
        logger.error(f"[save-icp] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/generate-icp")
async def generate_icp_no_db(body: ProfileCreate, user: dict = Depends(get_current_user)):
    """
    Generate 3 ICP options without writing to DB.
    Use this for local testing before Supabase is set up.
    """
    try:
        website_text = await profile_agent.deep_crawl_website(body.website_url)
        linkedin_text = await profile_agent.crawl_url(body.linkedin_url or "", char_limit=6000)

        # Deep ICP: gather external evidence (lookalikes + reviews) before synthesis
        from app.agents.icp_research import research_company, format_evidence
        from urllib.parse import urlparse
        company_name = urlparse(body.website_url).netloc.replace("www.", "").split(".")[0]
        research = await research_company(body.website_url, company_name, body.service_description)
        evidence = format_evidence(research)

        user_context, icp_options, usage = await profile_agent.generate_icp_options(
            website_text,
            linkedin_text,
            body.service_description,
            body.target_description,
            research_evidence=evidence,
        )

        return {
            "user_context": user_context,
            "icp_options": [opt.dict() for opt in icp_options],
            "_debug": {
                "website_chars": len(website_text),
                "linkedin_chars": len(linkedin_text),
                "website_preview": website_text if website_text else "EMPTY",
                "linkedin_preview": linkedin_text if linkedin_text else "EMPTY",
                "tokens_in": usage["input_tokens"],
                "tokens_out": usage["output_tokens"],
                "cost_usd": usage["cost_usd"],
                "research_evidence": evidence or "none gathered",
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"[generate-icp] Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="ICP generation failed. Please try again.")


@router.post("/")
async def create_profile(body: ProfileCreate, user: dict = Depends(get_current_user)):
    """
    Create a new profile and kick off the Profile Agent.
    Returns the 3 ICP options for user to choose from.
    """
    _assert_profile_quota(user)
    # 1. Create profile row — owned by the logged-in user
    result = supabase.table("user_profiles").insert({
        "user_id":             user["id"],
        "name":                body.name,
        "website_url":         body.website_url,
        "linkedin_url":        body.linkedin_url,
        "service_description": body.service_description,
        "target_description":  body.target_description,
    }).execute()

    profile_id = result.data[0]["id"]

    # 2. Run Profile Agent (crawl + generate ICP options)
    agent_result = await profile_agent.run(profile_id)

    return {
        "profile_id":   profile_id,
        "user_context": agent_result.get("user_context"),
        "icp_options":  agent_result.get("icp_options", []),
    }


@router.get("/list")
async def list_all_profiles(user: dict = Depends(get_current_user)):
    """The caller's own profiles — powers the workspace switcher dropdown.
    Admins (founders) see every profile. Newest first."""
    q = supabase.table("user_profiles").select("id, name, created_at")
    if not user.get("is_admin"):
        q = q.eq("user_id", user["id"])
    r = q.order("created_at", desc=True).limit(40).execute()
    return [{"id": p["id"], "name": p.get("name") or "Untitled"} for p in (r.data or [])]


@router.get("/{user_id}/all")
async def list_profiles(user_id: str, user: dict = Depends(get_current_user)):
    """Return all profiles for a user (only your own, unless admin)."""
    if not user.get("is_admin") and user_id != user["id"]:
        raise HTTPException(403, "You don't have access to these profiles.")
    result = supabase.table("user_profiles") \
        .select("id, name, service_description, is_active, created_at") \
        .eq("user_id", user_id) \
        .order("created_at") \
        .execute()
    return result.data or []


@router.post("/{profile_id}/seller-brain")
async def seller_brain(profile_id: str, body: dict, user: dict = Depends(owned_profile)):
    """Run the SELLER BRAIN from the intake answers (the 8 questions + dream companies):
    builds the deep dossier, persists it, and builds the precision Target List + dream targets.
    Body = the intake dict (or {"intake": {...}}). Takes ~30-60s."""
    intake = body.get("intake") if isinstance(body.get("intake"), dict) else body
    result = await profile_agent.build_seller_brain(profile_id, intake)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    # don't ship the whole dossier back by default — summary is enough for the UI
    return {
        "status": "ok",
        "precision_targets": result["precision_targets"],
        "dream_targets": result["dream_targets"],
        "segments": result["segments"],
        "offering": result["dossier"].get("offering"),
        "exa_queries": result["dossier"].get("exa_queries", []),
    }


@router.post("/onboard")
async def onboard(body: dict, user: dict = Depends(get_current_user)):
    """FULL onboarding from scratch (basic UI posts here):
    { username, website_url, intake: {the 8 questions + dream_companies} }
    → creates a profile OWNED BY THE LOGGED-IN USER, synthesizes ICP context + vector +
    search facets, stores best_clients as exclusions, then runs the Seller Brain (dossier +
    precision Target List). Returns the new profile_id. ~60-90s."""
    username = (body.get("username") or "").strip()
    website_url = (body.get("website_url") or "").strip()
    intake = body.get("intake") or {}
    if not website_url:
        raise HTTPException(status_code=400, detail="website_url is required")
    _assert_profile_quota(user)

    # 1. owned by the authenticated account; username is just the profile label
    user_id = user["id"]
    if not username:
        username = (user.get("email") or "My Profile").split("@")[0]

    # 2. synthesize ICP context from the intake answers
    icp_text = " | ".join(x for x in [
        intake.get("offering", ""),
        f"Ideal customer: {intake.get('ideal_customer', '')}",
        f"Buyer: {intake.get('buyer', '')}",
        f"Triggers: {intake.get('need_trigger', '')}",
    ] if x and x.strip(": "))
    user_context = intake.get("offering", "") or icp_text

    from app.pipeline.matching import vectorise_text
    icp_vector = await vectorise_text(icp_text)
    search_profile = await profile_agent.build_search_profile(icp_text, user_context)
    if intake.get("delivery_model"):
        search_profile["seller_delivery_model"] = intake["delivery_model"]

    # 3. create the profile
    prof = supabase.table("user_profiles").insert({
        "user_id": user_id,
        "name": username,
        "website_url": website_url,
        "service_description": intake.get("offering", ""),
        "target_description": intake.get("ideal_customer", ""),
        "user_context": user_context,
        "icp_text": icp_text,
        "icp_vector": icp_vector,
        "search_profile": search_profile,
    }).execute()
    profile_id = prof.data[0]["id"]
    logger.info(f"[onboard] created profile {profile_id} for '{username}'")

    # 4. seller's named best clients → exclude from leads
    for c in (intake.get("best_clients") or []):
        try:
            supabase.table("existing_clients").upsert(
                {"profile_id": profile_id, "company_name": c},
                on_conflict="profile_id,company_name").execute()
        except Exception:
            pass

    # 5. Seller Brain — dossier + precision Target List + dream targets
    brain = await profile_agent.build_seller_brain(profile_id, intake)

    return {
        "status": "ok",
        "profile_id": profile_id,
        "username": username,
        "precision_targets": brain.get("precision_targets", 0),
        "dream_targets": brain.get("dream_targets", 0),
        "segments": brain.get("segments", []),
    }


@router.post("/{profile_id}/refine")
async def refine(profile_id: str, body: dict, background_tasks: BackgroundTasks, user: dict = Depends(owned_profile)):
    """Conversational dossier editor ('Ask cnvrted'). Applies the seller's NL feedback to
    the dossier, drops excluded companies instantly, and schedules a precision Target List
    rebuild in the background when the change is structural. Returns {reply, rebuilding, removed}."""
    res = await profile_agent.refine_dossier(profile_id, body.get("message", ""))
    if res.get("rebuilding"):
        background_tasks.add_task(profile_agent.rebuild_precision, profile_id)
    return res


@router.post("/{profile_id}/refine/undo")
async def refine_undo(profile_id: str, background_tasks: BackgroundTasks, user: dict = Depends(owned_profile)):
    res = await profile_agent.undo_refine(profile_id)
    if res.get("rebuilding"):
        background_tasks.add_task(profile_agent.rebuild_precision, profile_id)
    return res


@router.post("/{profile_id}/approve")
async def approve_icp(profile_id: str, body: ICPApproval, user: dict = Depends(owned_profile)):
    """User picked an ICP option. Store it and generate vector."""
    result = await profile_agent.approve_icp(profile_id, body.chosen_icp_text)
    return result


@router.post("/{profile_id}/chat")
async def icp_chat(profile_id: str, body: ICPChatMessage, user: dict = Depends(owned_profile)):
    """
    ICP building chatbot — when user rejects all 3 options.
    Claude asks questions, builds custom ICP collaboratively.

    TODO: implement chat logic in profile_agent.py
    """
    # TODO: implement
    return {"message": "ICP chat coming soon", "type": "chat"}


# Columns a client must never be able to set via the profile-update body (ownership
# reassignment, identity, server-managed vectors/timestamps). Everything else is editable.
_PROTECTED_PROFILE_FIELDS = {"id", "user_id", "created_at", "updated_at", "icp_vector"}


@router.put("/{profile_id}")
async def update_profile(profile_id: str, body: dict, user: dict = Depends(owned_profile)):
    """Update profile fields. Re-runs Profile Agent if ICP-related fields change."""
    # Whitelist by dropping protected keys — prevents mass-assignment (e.g. reassigning
    # user_id to hijack ownership, or clobbering the server-managed vector).
    clean = {k: v for k, v in (body or {}).items() if k not in _PROTECTED_PROFILE_FIELDS}
    if clean:
        supabase.table("user_profiles").update(clean).eq("id", profile_id).execute()
    return {"status": "updated"}


@router.delete("/{profile_id}")
async def delete_profile(profile_id: str, user: dict = Depends(owned_profile)):
    supabase.table("user_profiles").delete().eq("id", profile_id).execute()
    return {"status": "deleted"}
