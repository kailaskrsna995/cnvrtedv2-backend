"""
NOTION EXPORT
=============
Pushes a lead/target list into the founder's Notion as a fresh database (one row per item).
Uses an internal integration token (single workspace) — multi-user OAuth is a later upgrade.

Frontend sends { title, columns: [{name, type}], rows: [ {colName: value} ] }.
type ∈ title | url | number | email | text. Exactly one 'title' column (else first becomes title).
"""

import logging
import httpx
from app.config import NOTION_API_KEY, NOTION_PARENT_PAGE_ID

logger = logging.getLogger(__name__)
NOTION_VERSION = "2022-06-28"
BASE = "https://api.notion.com/v1"
MAX_ROWS = 80  # keep the export snappy + within rate limits


def _headers():
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _schema_for(col_type: str):
    return {
        "title": {"title": {}},
        "url": {"url": {}},
        "number": {"number": {}},
        "email": {"email": {}},
    }.get(col_type, {"rich_text": {}})


def _value_for(col_type: str, value):
    s = "" if value is None else str(value)
    if col_type == "title":
        return {"title": [{"text": {"content": s[:1900]}}]}
    if col_type == "url":
        v = s.strip()
        if v and not v.startswith("http"):
            v = "https://" + v
        return {"url": v or None}
    if col_type == "email":
        return {"email": (s.strip() or None)}
    if col_type == "number":
        try:
            return {"number": float(value)}
        except (TypeError, ValueError):
            return {"number": None}
    return {"rich_text": [{"text": {"content": s[:1900]}}]}


async def export_to_notion(title: str, columns: list, rows: list) -> dict:
    if not NOTION_API_KEY or not NOTION_PARENT_PAGE_ID:
        return {"error": "Notion not configured (set NOTION_API_KEY + NOTION_PARENT_PAGE_ID)"}
    if not columns:
        return {"error": "no columns"}

    # ensure exactly one title column
    if not any(c.get("type") == "title" for c in columns):
        columns = [{**columns[0], "type": "title"}] + columns[1:]

    props_schema = {c["name"]: _schema_for(c.get("type", "text")) for c in columns}

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            # 1. create the database under the shared parent page
            db = await http.post(f"{BASE}/databases", headers=_headers(), json={
                "parent": {"type": "page_id", "page_id": NOTION_PARENT_PAGE_ID},
                "title": [{"type": "text", "text": {"content": title[:100]}}],
                "properties": props_schema,
            })
            if db.status_code != 200:
                logger.error(f"[notion] create db {db.status_code}: {db.text[:300]}")
                return {"error": f"Notion DB create failed ({db.status_code}). Is the page shared with the integration?"}
            db_json = db.json()
            db_id = db_json["id"]
            url = db_json.get("url")

            # 2. one page (row) per item
            written = 0
            for row in rows[:MAX_ROWS]:
                page_props = {c["name"]: _value_for(c.get("type", "text"), row.get(c["name"]))
                              for c in columns}
                p = await http.post(f"{BASE}/pages", headers=_headers(), json={
                    "parent": {"database_id": db_id},
                    "properties": page_props,
                })
                if p.status_code == 200:
                    written += 1
                else:
                    logger.warning(f"[notion] page {p.status_code}: {p.text[:160]}")

            return {"url": url, "written": written, "total": len(rows)}
    except Exception as e:
        logger.error(f"[notion] export failed: {e}")
        return {"error": str(e)}
