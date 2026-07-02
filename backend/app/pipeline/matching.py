"""
MATCHING ENGINE
===============
Takes a signal and finds which user profiles care about it.
Uses pgvector cosine similarity — pure maths, no AI, extremely fast.

Flow per signal:
  1. Vectorise signal text (OpenAI)
  2. pgvector query: which ICP vectors are within threshold?
  3. Return [(profile_id, user_id, similarity_score)]

Scale: one query regardless of how many users exist.
"""

import logging
from openai import AsyncOpenAI
from app.database import supabase
from app.config import VECTOR_SIMILARITY_THRESHOLD, OPENAI_API_KEY
from app import usage

logger = logging.getLogger(__name__)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
EMBEDDING_MODEL = "text-embedding-3-small"


async def vectorise_text(text: str) -> list[float]:
    """
    Convert any text to a 1536-dim vector using OpenAI text-embedding-3-small.
    Used for BOTH ICP vectors and signal vectors — must be the same model.
    Cost: ~$0.02 per 1M tokens. Negligible.
    """
    if not text or not text.strip():
        return []
    try:
        response = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text.strip(),
        )
        try:
            usage.log_openai_embedding(EMBEDDING_MODEL, getattr(response.usage, "total_tokens", 0))
        except Exception:
            pass
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"[Matching] OpenAI embedding failed: {e}")
        return []


async def find_matching_profiles(signal_text: str) -> list[dict]:
    """
    Core matching function.
    Returns list of { profile_id, user_id, similarity, icp_text, user_context } dicts.
    Called by the pipeline worker for every signal that exits the queue.
    """
    if not supabase:
        logger.warning("[Matching] Supabase not configured — skipping match")
        return []

    # 1. Vectorise the signal
    signal_vector = await vectorise_text(signal_text)
    if not signal_vector:
        logger.warning("[Matching] Could not vectorise signal text")
        return []

    # 2. Query pgvector via Supabase RPC
    try:
        result = supabase.rpc("match_profiles", {
            "query_vector": signal_vector,
            "match_threshold": VECTOR_SIMILARITY_THRESHOLD,
            "match_count": 100
        }).execute()

        matched = result.data or []
        if not matched:
            return []

        # 3. Fetch ICP text + user_context for each matched profile (needed for scoring)
        profile_ids = [m["profile_id"] for m in matched]
        profiles_result = supabase.table("user_profiles") \
            .select("id, user_id, icp_text, user_context") \
            .in_("id", profile_ids) \
            .execute()

        profiles_map = {p["id"]: p for p in (profiles_result.data or [])}

        # 4. Merge similarity score with profile data
        enriched = []
        for m in matched:
            profile = profiles_map.get(m["profile_id"], {})
            enriched.append({
                "profile_id": m["profile_id"],
                "user_id": m["user_id"],
                "similarity": m["similarity"],
                "icp_text": profile.get("icp_text", ""),
                "user_context": profile.get("user_context", ""),
            })

        logger.info(f"[Matching] Signal matched {len(enriched)} profiles")
        return enriched

    except Exception as e:
        logger.error(f"[Matching] pgvector query failed: {e}")
        return []
