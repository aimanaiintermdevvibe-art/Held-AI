import json
import logging
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import asyncpg
import httpx

from backend.core.config import get_settings
from backend.db.connection import get_db_pool

logger = logging.getLogger("held_ai.grief_notes")

# ==============================================================================
# Grief notes (migration 005): the agent's copy of each user's Held notes, and the
# mood catalogue.
# Every query is scoped to the owning user.
# ==============================================================================

NOTE_COLUMNS = """
    n.id, n.user_id, n.note_date, n.content, n.mood_id, n.mood,
    n.held_created_at, n.held_updated_at, n.synced_at
"""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _note_dict(row: Any) -> Dict[str, Any]:
    """A stored note in Held's shape (camelCase)."""
    return {
        "id": row["id"],
        "date": row["note_date"].isoformat(),
        "content": row["content"],
        "moodId": row["mood_id"],
        "mood": _json(row["mood"]),
        "createdAt": row["held_created_at"].isoformat() if row["held_created_at"] else None,
        "updatedAt": row["held_updated_at"].isoformat() if row["held_updated_at"] else None,
        "syncedAt": row["synced_at"].isoformat() if row["synced_at"] else None,
    }


async def _pool() -> asyncpg.Pool:
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")
    return pool


async def store_notes(user_id: str, notes: List[Dict[str, Any]]) -> None:
    """Insert or update Held notes for the user."""
    if not notes:
        return
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for note in notes:
                note_date = date.fromisoformat(note["date"][:10])
                # Held keeps one note per date; a local copy under another id is stale.
                await conn.execute(
                    "DELETE FROM public.grief_notes WHERE user_id = $1 AND note_date = $2 AND id <> $3;",
                    user_id, note_date, note["id"],
                )
                await conn.execute(
                    """
                    INSERT INTO public.grief_notes (
                        id, user_id, note_date, content, mood_id, mood,
                        held_created_at, held_updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                    ON CONFLICT (id) DO UPDATE SET
                        note_date = EXCLUDED.note_date,
                        content = EXCLUDED.content,
                        mood_id = EXCLUDED.mood_id,
                        mood = EXCLUDED.mood,
                        held_created_at = EXCLUDED.held_created_at,
                        held_updated_at = EXCLUDED.held_updated_at,
                        synced_at = now();
                    """,
                    note["id"], user_id, note_date, note.get("content") or "", note.get("moodId"),
                    json.dumps(note.get("mood")) if note.get("mood") else None,
                    _timestamp(note.get("createdAt")), _timestamp(note.get("updatedAt")),
                )


async def get_note(user_id: str, note_date: date) -> Optional[Dict[str, Any]]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {NOTE_COLUMNS} FROM public.grief_notes n WHERE n.user_id = $1 AND n.note_date = $2;",
            user_id, note_date,
        )
        return _note_dict(row) if row else None


async def list_notes(
    user_id: str,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    page: int = 1,
    limit: int = 20,
) -> tuple[List[Dict[str, Any]], int]:
    """A page of the user's stored notes, newest first, and the total in the date range."""
    pool = await _pool()
    async with pool.acquire() as conn:
        where = "n.user_id = $1 AND ($2::date IS NULL OR n.note_date >= $2) AND ($3::date IS NULL OR n.note_date <= $3)"
        total = await conn.fetchval(f"SELECT count(*) FROM public.grief_notes n WHERE {where};", user_id, date_from, date_to)
        rows = await conn.fetch(
            f"SELECT {NOTE_COLUMNS} FROM public.grief_notes n WHERE {where} "
            "ORDER BY n.note_date DESC LIMIT $4 OFFSET $5;",
            user_id, date_from, date_to, limit, (page - 1) * limit,
        )
        return [_note_dict(r) for r in rows], int(total)


async def list_moods() -> List[Dict[str, Any]]:
    if get_settings().database_schema == "held":
        return await _held_moods()
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT id, code, title, "imageUrl" FROM public."Mood" ORDER BY title;')
        return [dict(r) for r in rows]


_MOODS_TTL_SECONDS = 600
_held_moods_cache: Dict[str, Any] = {"at": 0.0, "moods": None}


async def _held_moods() -> List[Dict[str, Any]]:
    """Held's public mood list, by title (on the Held app's own database this user cannot read
    "Mood"). Kept for 10 minutes, since the list rarely changes."""
    if _held_moods_cache["moods"] is not None and time.monotonic() - _held_moods_cache["at"] < _MOODS_TTL_SECONDS:
        return _held_moods_cache["moods"]
    base_url = get_settings().held_api_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(f"{base_url}/moods")
            res.raise_for_status()
            data = res.json().get("data")
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("The Held mood list is unavailable.") from exc
    if not isinstance(data, list):
        raise RuntimeError("The Held mood list is unavailable.")
    moods = [{"id": m["id"], "code": m["code"], "title": m["title"], "imageUrl": m.get("imageUrl")}
             for m in data if isinstance(m, dict) and m.get("id") and m.get("code") and m.get("title")]
    moods = sorted(moods, key=lambda m: m["title"])
    _held_moods_cache.update(at=time.monotonic(), moods=moods)
    return moods


async def upsert_moods(moods: List[Dict[str, Any]]) -> Dict[str, int]:
    """Insert or update catalogue entries by id; entries not in the list are left alone."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = {r["id"]: tuple(r[1:]) for r in await conn.fetch(
                'SELECT id, code, title, "imageUrl" FROM public."Mood";'
            )}
            await conn.executemany(
                """
                INSERT INTO public."Mood" (id, code, title, "imageUrl") VALUES ($1, $2, $3, $4)
                ON CONFLICT (id) DO UPDATE
                SET code = EXCLUDED.code, title = EXCLUDED.title, "imageUrl" = EXCLUDED."imageUrl",
                    "updatedAt" = CURRENT_TIMESTAMP
                WHERE (public."Mood".code, public."Mood".title, public."Mood"."imageUrl")
                      IS DISTINCT FROM (EXCLUDED.code, EXCLUDED.title, EXCLUDED."imageUrl");
                """,
                [(m["id"], m["code"], m["title"], m.get("imageUrl")) for m in moods],
            )
    added = sum(1 for m in moods if m["id"] not in before)
    changed = sum(1 for m in moods if m["id"] in before and before[m["id"]] != (m["code"], m["title"], m.get("imageUrl")))
    return {"added": added, "updated": changed, "unchanged": len(moods) - added - changed}
