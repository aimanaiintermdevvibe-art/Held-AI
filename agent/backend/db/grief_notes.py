import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import asyncpg

from backend.db.connection import get_db_pool

logger = logging.getLogger("held_ai.grief_notes")

# ==============================================================================
# Grief notes (migration 005): the agent's copy of each user's Held notes, with
# the workbook exercise a note answers, and the mood catalogue.
# Every query is scoped to the owning user.
# ==============================================================================

NOTE_COLUMNS = """
    n.id, n.user_id, n.note_date, n.content, n.mood_id, n.mood, n.exercise_key,
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
    """A stored note in Held's shape (camelCase), plus the workbook exercise key."""
    return {
        "id": row["id"],
        "date": row["note_date"].isoformat(),
        "content": row["content"],
        "moodId": row["mood_id"],
        "mood": _json(row["mood"]),
        "exerciseKey": row["exercise_key"],
        "createdAt": row["held_created_at"].isoformat() if row["held_created_at"] else None,
        "updatedAt": row["held_updated_at"].isoformat() if row["held_updated_at"] else None,
        "syncedAt": row["synced_at"].isoformat() if row["synced_at"] else None,
    }


async def _pool() -> asyncpg.Pool:
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")
    return pool


async def store_notes(user_id: str, notes: List[Dict[str, Any]], exercise_key: Optional[str] = None) -> None:
    """Insert or update Held notes for the user. exercise_key, when given, is set on all of
    them; otherwise a note keeps the exercise it already had."""
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
                        id, user_id, note_date, content, mood_id, mood, exercise_key,
                        held_created_at, held_updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
                    ON CONFLICT (id) DO UPDATE SET
                        note_date = EXCLUDED.note_date,
                        content = EXCLUDED.content,
                        mood_id = EXCLUDED.mood_id,
                        mood = EXCLUDED.mood,
                        exercise_key = COALESCE(EXCLUDED.exercise_key, public.grief_notes.exercise_key),
                        held_created_at = EXCLUDED.held_created_at,
                        held_updated_at = EXCLUDED.held_updated_at,
                        synced_at = now();
                    """,
                    note["id"], user_id, note_date, note.get("content") or "", note.get("moodId"),
                    json.dumps(note.get("mood")) if note.get("mood") else None, exercise_key,
                    _timestamp(note.get("createdAt")), _timestamp(note.get("updatedAt")),
                )


async def set_exercise(user_id: str, note_date: date, exercise_key: Optional[str]) -> Optional[Dict[str, Any]]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE public.grief_notes n SET exercise_key = $3 WHERE user_id = $1 AND note_date = $2 "
            f"RETURNING {NOTE_COLUMNS};",
            user_id, note_date, exercise_key,
        )
        return _note_dict(row) if row else None


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
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT id, code, title, "imageUrl" FROM public."Mood" ORDER BY title;')
        return [dict(r) for r in rows]


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
