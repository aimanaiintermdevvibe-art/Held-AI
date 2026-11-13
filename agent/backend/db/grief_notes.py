"""Read-only queries for the grief note chat tools (backend.grief.note_tools): only "GriefNote"
(date, content, mood) and "Mood" (code, title), always for one user."""
from datetime import date
from typing import Any, Dict, List, Optional

from backend.db.connection import get_db_pool


async def _pool():
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")
    return pool


# ==============================================================================
# Grief note tools (backend.grief.note_tools). Only "GriefNote" (date, content, mood) and "Mood"
# (code, title) are read, always for one user; the callers bound and validate every argument.
# ==============================================================================

_NOTE_WITH_MOOD = """
    SELECT g.date, left(g.content, $%d) AS text, m.title AS mood
    FROM "GriefNote" g LEFT JOIN "Mood" m ON m.id = g."moodId"
"""


async def notes_between(user_id: str, start: date, end: date, limit: int, max_chars: int) -> List[Dict[str, Any]]:
    """The user's notes dated start to end (both included), newest first, with each mood's title."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _NOTE_WITH_MOOD % 5 + """
            WHERE g."userId" = $1 AND g.date BETWEEN $2 AND $3
            ORDER BY g.date DESC
            LIMIT $4
            """,
            user_id, start, end, limit, max_chars,
        )
    return [{"date": r["date"].isoformat(), "mood": r["mood"], "text": r["text"]} for r in rows]


async def note_on(user_id: str, day: date, max_chars: int) -> Optional[Dict[str, Any]]:
    """The user's note for one date (there is at most one per day), or None."""
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _NOTE_WITH_MOOD % 3 + """
            WHERE g."userId" = $1 AND g.date = $2
            """,
            user_id, day, max_chars,
        )
    return {"date": row["date"].isoformat(), "mood": row["mood"], "text": row["text"]} if row else None


async def mood_counts_by_week(user_id: str, start: date, end: date) -> List[Dict[str, Any]]:
    """How many of the user's notes dated start to end carry each mood, per week (Monday start)."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date_trunc('week', g.date)::date AS week_start, m.title AS mood, count(*)::int AS count
            FROM "GriefNote" g LEFT JOIN "Mood" m ON m.id = g."moodId"
            WHERE g."userId" = $1 AND g.date BETWEEN $2 AND $3
            GROUP BY 1, 2
            ORDER BY 1 DESC, 3 DESC
            """,
            user_id, start, end,
        )
    return [{"week_start": r["week_start"].isoformat(), "mood": r["mood"], "count": r["count"]} for r in rows]

