"""The repository functions for the Held app's own development database (DATABASE_SCHEMA=held).

That database is managed by the Held app (Prisma). The agent's database user may read and write
"ChatSession" and "ChatMessage", only read "User", and cannot create tables or columns. So:

- Chats go into "ChatSession" / "ChatMessage" with the app's column names. Ids are UUID text
  made here, and timestamps are written in UTC (the columns have no time zone, and the server's
  own clock is not UTC).
- The rolling summary, its anchor and the message count have their own "ChatSession" columns
  ("sessionSummary", "lastSummarizedMessageId", "totalMessageCount"). The user memory has none,
  so it lives in the chat's jsonb metadata under the reserved key "memory" ({"user_memory"}),
  which clients never see, and is kept on the user's newest chat.
- This user may not delete chats there; deleting one raises PermissionError.
- Users are created by the Held app, never here. The disease list comes from Held's API, because
  this user cannot read "Disease".
- Grief notes are read from the app's "GriefNote" table (read only) and written through Held's API.

Every function matches the one of the same name in repository.py, which calls it here.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import asyncpg
import httpx

from backend.core.config import get_settings
from backend.db.connection import get_db_pool

logger = logging.getLogger("held_ai.held_schema")

NOW = "(now() AT TIME ZONE 'UTC')"
MEMORY_KEY = "memory"  # the agent's own data inside "ChatSession".metadata (the user memory)
SESSION_COLUMNS = """
    s.id, s."userId", s.title, s.status::text AS status, s.metadata, s."createdAt", s."updatedAt",
    s."sessionSummary", s."lastSummarizedMessageId", s."totalMessageCount"
"""

__all__ = [
    "list_chat_sessions", "create_chat_session", "get_chat_session", "save_session_summary",
    "delete_chat_session", "get_session_messages", "open_chat", "get_messages_after", "save_turn",
    "save_user_memory", "get_previous_conversations", "list_users", "get_user_by_id", "get_auth_user",
    "create_user_from_profile", "list_diseases", "upsert_diseases", "ping_database", "get_database_overview",
]


def _key(value: Any) -> Optional[str]:
    """A client-supplied id: the app's ids are plain text, so any short non-empty string."""
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        return None
    return value.strip()


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _iso(value: Any) -> Optional[str]:
    """A stored UTC timestamp as ISO 8601 with its +00:00 offset."""
    if not value:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _session_dict(row: Any) -> Dict[str, Any]:
    metadata = _json(row["metadata"])
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata.pop(MEMORY_KEY, None)
    return {
        "id": row["id"],
        "user_id": row["userId"],
        "title": row["title"],
        "status": row["status"],
        "metadata": metadata,
        "session_summary": row["sessionSummary"],
        "last_summarized_message_id": row["lastSummarizedMessageId"],
        "total_message_count": int(row["totalMessageCount"] or 0),
        "created_at": _iso(row["createdAt"]),
        "updated_at": _iso(row["updatedAt"]),
    }


def _memory_messages(value: Any) -> List[Dict[str, Any]]:
    return [
        {"id": m["id"], "role": m["role"], "content": m["content"], "created_at": _iso(m.get("created_at"))}
        for m in _json(value) or []
    ]


def _set_memory(fields: str) -> str:
    """SQL that merges jsonb_build_object(fields) into metadata -> 'memory'."""
    return (
        f"COALESCE(metadata, '{{}}'::jsonb) || jsonb_build_object('{MEMORY_KEY}', "
        f"COALESCE(metadata -> '{MEMORY_KEY}', '{{}}'::jsonb) || jsonb_build_object({fields}))"
    )


# ==============================================================================
# Chats
# ==============================================================================

async def list_chat_sessions(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {SESSION_COLUMNS},
                   (SELECT MAX(m."createdAt") FROM "ChatMessage" m WHERE m."sessionId" = s.id) AS last_message_at
            FROM "ChatSession" s
            WHERE ($1::text IS NULL OR s."userId" = $1)
            ORDER BY s."updatedAt" DESC, s."createdAt" DESC
            """,
            user_id.strip() if user_id else None,
        )
    result = []
    for r in rows:
        session = _session_dict(r)
        session["message_count"] = session["total_message_count"]
        session["last_message_at"] = _iso(r["last_message_at"])
        result.append(session)
    return result


async def create_chat_session(
    user_id: Optional[str] = None, title: str = "New Conversation", metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool()
    if not pool:
        return None
    if not user_id or not isinstance(user_id, str) or not user_id.strip():
        logger.warning("Refusing to create chat session for a missing user id.")
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO "ChatSession" AS s (id, "userId", title, status, metadata, "createdAt", "updatedAt")
            SELECT $1, u.id, $2, 'ACTIVE', COALESCE($3::jsonb, '{{}}'::jsonb), {NOW}, {NOW}
            FROM "User" u WHERE u.id = $4
            RETURNING {SESSION_COLUMNS}
            """,
            str(uuid4()), title, json.dumps(metadata) if metadata else None, user_id.strip(),
        )
    if not row:
        logger.warning("Refusing to create chat session for an unknown user id.")
        return None
    return _session_dict(row)


async def get_chat_session(session_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool()
    key = _key(session_id)
    if not pool or not key:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f'SELECT {SESSION_COLUMNS} FROM "ChatSession" s WHERE s.id = $1 AND ($2::text IS NULL OR s."userId" = $2)',
            key, user_id,
        )
    return _session_dict(row) if row else None


async def save_session_summary(
    session_id: str, session_summary: str, last_summarized_message_id: str, previous_anchor: Optional[str] = None,
) -> bool:
    pool = await get_db_pool()
    key = _key(session_id)
    if not pool or not key:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"""
            UPDATE "ChatSession" SET "sessionSummary" = $2, "lastSummarizedMessageId" = $3
            WHERE id = $1 AND "lastSummarizedMessageId" IS NOT DISTINCT FROM $4::text
            """,
            key, session_summary, last_summarized_message_id, previous_anchor,
        )
    return result == "UPDATE 1"


async def delete_chat_session(session_id: str, user_id: Optional[str] = None) -> bool:
    pool = await get_db_pool()
    key = _key(session_id)
    if not pool or not key:
        return False
    async with pool.acquire() as conn:
        try:
            result = await conn.execute(
                'DELETE FROM "ChatSession" WHERE id = $1 AND ($2::text IS NULL OR "userId" = $2)', key, user_id,
            )
        except asyncpg.InsufficientPrivilegeError as exc:
            raise PermissionError("Chats cannot be deleted on this database.") from exc
    return result.startswith("DELETE") and not result.endswith(" 0")


async def get_session_messages(session_id: str) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    key = _key(session_id)
    if not pool or not key:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT id, "sessionId", role, content, metadata, "createdAt" FROM "ChatMessage" '
            'WHERE "sessionId" = $1 ORDER BY "createdAt", id',
            key,
        )
    return [
        {"id": r["id"], "session_id": r["sessionId"], "role": r["role"], "content": r["content"],
         "metadata": _json(r["metadata"]) or {}, "created_at": _iso(r["createdAt"])}
        for r in rows
    ]


async def open_chat(
    session_id: str, user_id: Optional[str] = None, recent: int = 20,
) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]], Optional[str]]]:
    """The session, its latest `recent` messages oldest first, and the user memory, in one query.
    The user memory is the one kept on the user's newest chat that has one."""
    pool = await get_db_pool()
    key = _key(session_id)
    if not pool or not key:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {SESSION_COLUMNS},
                   (SELECT json_agg(json_build_object('id', m.id, 'role', m.role, 'content', m.content,
                                                      'created_at', m."createdAt") ORDER BY m."createdAt", m.id)
                    FROM (SELECT id, role, content, "createdAt" FROM "ChatMessage"
                          WHERE "sessionId" = s.id ORDER BY "createdAt" DESC, id DESC LIMIT $3) m) AS recent,
                   (SELECT o.metadata -> '{MEMORY_KEY}' ->> 'user_memory' FROM "ChatSession" o
                    WHERE o."userId" = s."userId" AND o.metadata -> '{MEMORY_KEY}' ? 'user_memory'
                    ORDER BY o."createdAt" DESC LIMIT 1) AS user_memory
            FROM "ChatSession" s
            WHERE s.id = $1 AND ($2::text IS NULL OR s."userId" = $2)
            """,
            key, user_id, recent,
        )
    if not row:
        return None
    return _session_dict(row), _memory_messages(row["recent"]), row["user_memory"]


async def get_messages_after(session_id: str, anchor_id: Optional[str], limit: int = 200) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    key = _key(session_id)
    if not pool or not key:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.id, m.role, m.content, m."createdAt"
            FROM "ChatMessage" m
            WHERE m."sessionId" = $1
              AND ($2::text IS NULL OR (m."createdAt", m.id) > (
                    SELECT a."createdAt", a.id FROM "ChatMessage" a WHERE a.id = $2))
            ORDER BY m."createdAt", m.id
            LIMIT $3
            """,
            key, _key(anchor_id), limit,
        )
    return [{"id": r["id"], "role": r["role"], "content": r["content"], "created_at": _iso(r["createdAt"])} for r in rows]


async def save_turn(
    session_id: str, prompt: str, response: str, title: Optional[str] = None,
    reply_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Both messages, the chat's updatedAt and (for a new chat) its title, in one statement. The
    columns keep milliseconds, so the reply is stamped one millisecond after the prompt.
    reply_metadata (e.g. the reply's token use) goes into the reply's metadata."""
    pool = await get_db_pool()
    key = _key(session_id)
    if not pool or not key:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            WITH t AS (SELECT {NOW} AS at),
            m AS (
                INSERT INTO "ChatMessage" (id, "sessionId", role, content, metadata, "createdAt")
                SELECT $2, $1, 'user', $4, '{{}}'::jsonb, t.at FROM t
                UNION ALL
                SELECT $3, $1, 'assistant', $5, COALESCE($7::jsonb, '{{}}'::jsonb), t.at + interval '1 millisecond' FROM t
                RETURNING id, role, content, "createdAt"
            ), s AS (
                UPDATE "ChatSession"
                SET "updatedAt" = (SELECT at FROM t), title = COALESCE($6, title),
                    "totalMessageCount" = "totalMessageCount" + 2
                WHERE id = $1
                RETURNING title, "updatedAt", "totalMessageCount"
            )
            SELECT (SELECT json_agg(json_build_object('id', m.id, 'role', m.role, 'content', m.content,
                                                      'created_at', m."createdAt") ORDER BY m."createdAt") FROM m) AS messages,
                   s."totalMessageCount", s.title, s."updatedAt"
            FROM s
            """,
            key, str(uuid4()), str(uuid4()), prompt, response, title,
            json.dumps(reply_metadata) if reply_metadata else None,
        )
    if not row:
        return None
    return {
        "messages": _memory_messages(row["messages"]),
        "total_message_count": int(row["totalMessageCount"] or 0),
        "title": row["title"],
        "updated_at": _iso(row["updatedAt"]),
    }


async def save_user_memory(user_id: str, summary: str) -> None:
    """Keep the user memory on the user's newest chat (there is no table of its own here)."""
    pool = await get_db_pool()
    if not pool or not user_id:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            UPDATE "ChatSession" SET metadata = {_set_memory("'user_memory', $2::text")}
            WHERE id = (SELECT id FROM "ChatSession" WHERE "userId" = $1 ORDER BY "createdAt" DESC LIMIT 1)
            """,
            user_id, summary,
        )


async def get_previous_conversations(
    user_id: str, exclude_session_id: Optional[str] = None, limit: int = 10, recent_messages: int = 6,
    content_chars: int = 600,
) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    if not pool or not user_id:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT s.id, s.title, s."sessionSummary" AS summary, s."updatedAt",
                   (SELECT json_agg(json_build_object('role', m.role, 'content', left(m.content, $5))
                                    ORDER BY m."createdAt", m.id)
                    FROM (SELECT role, content, "createdAt", id FROM "ChatMessage"
                          WHERE "sessionId" = s.id AND role = 'user'
                          ORDER BY "createdAt" DESC, id DESC LIMIT $4) m) AS recent,
                   (SELECT json_agg(json_build_object('role', m.role, 'content', left(m.content, $5),
                                                      'created_at', m."createdAt") ORDER BY m."createdAt", m.id)
                    FROM (SELECT role, content, "createdAt", id FROM "ChatMessage"
                          WHERE "sessionId" = s.id AND role = 'user'
                          ORDER BY "createdAt" DESC, id DESC LIMIT 50) m) AS user_messages
            FROM "ChatSession" s
            WHERE s."userId" = $1
              AND ($2::text IS NULL OR s.id <> $2)
              AND EXISTS (SELECT 1 FROM "ChatMessage" m WHERE m."sessionId" = s.id)
            ORDER BY s."updatedAt" DESC
            LIMIT $3
            """,
            user_id.strip(), _key(exclude_session_id), limit, recent_messages, content_chars,
        )
    return [
        {
            "title": r["title"],
            "last_active": _iso(r["updatedAt"]),
            "summary": r["summary"],
            "recent_messages": _json(r["recent"]) or [],
            "user_messages": [{**m, "created_at": _iso(m.get("created_at"))} for m in _json(r["user_messages"]) or []],
        }
        for r in rows
    ]


# ==============================================================================
# Grief notes (read only: the Held app's "GriefNote" table; notes are written through Held's API)
# ==============================================================================

def _grief_note(row: Any) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "date": row["date"].isoformat(),
        "content": row["content"],
        "moodId": row["moodId"],
        "createdAt": _iso(row["createdAt"]),
        "updatedAt": _iso(row["updatedAt"]),
    }


async def list_grief_notes(
    user_id: str, date_from: Optional[Any] = None, date_to: Optional[Any] = None, page: int = 1, limit: int = 20,
) -> Tuple[List[Dict[str, Any]], int]:
    """A page of the user's notes, newest first, and the total in the date range."""
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")
    where = '"userId" = $1 AND ($2::date IS NULL OR date >= $2) AND ($3::date IS NULL OR date <= $3)'
    async with pool.acquire() as conn:
        total = await conn.fetchval(f'SELECT count(*) FROM "GriefNote" WHERE {where}', user_id, date_from, date_to)
        rows = await conn.fetch(
            f'SELECT id, date, content, "moodId", "createdAt", "updatedAt" FROM "GriefNote" WHERE {where} '
            "ORDER BY date DESC LIMIT $4 OFFSET $5",
            user_id, date_from, date_to, limit, (page - 1) * limit,
        )
    return [_grief_note(r) for r in rows], int(total or 0)


async def get_grief_note(user_id: str, note_date: Any) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, date, content, "moodId", "createdAt", "updatedAt" FROM "GriefNote" WHERE "userId" = $1 AND date = $2',
            user_id, note_date,
        )
    return _grief_note(row) if row else None


# ==============================================================================
# Users (read only: the Held app creates them) and the disease list
# ==============================================================================

USER_COLUMNS = 'id, email, "fullName", username, role::text AS role, relation::text AS relation, "createdAt"'


async def list_users(limit: int = 50) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(f'SELECT {USER_COLUMNS} FROM "User" ORDER BY "createdAt" DESC LIMIT $1', limit)
    return [{**dict(r), "createdAt": r["createdAt"].isoformat() if r["createdAt"] else None} for r in rows]


async def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f'SELECT {USER_COLUMNS} FROM "User" WHERE id = $1', user_id.strip())
    return dict(row) if row else None


async def get_auth_user(user_id: str) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT id, role::text AS role FROM "User" WHERE id = $1', user_id.strip())
    return dict(row) if row else None


async def create_user_from_profile(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Users are created by the Held app in this database; the agent only looks them up."""
    if not isinstance(profile.get("id"), str):
        return None
    user = await get_auth_user(profile["id"])
    if not user:
        logger.warning("Held user %s is not in the Held database; the agent cannot add users there.", profile["id"])
    return user


async def list_diseases() -> List[Dict[str, Any]]:
    """Held's public disease list, by name (this database user cannot read "Disease")."""
    base_url = get_settings().held_api_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(f"{base_url}/diseases")
            res.raise_for_status()
            data = res.json().get("data")
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("The Held disease list is unavailable.") from exc
    if not isinstance(data, list):
        raise RuntimeError("The Held disease list is unavailable.")
    diseases = [
        {"id": d["id"], "name": d["name"], "abbreviation": d.get("abbreviation"), "imageUrl": d.get("imageUrl")}
        for d in data if isinstance(d, dict) and d.get("id") and d.get("name")
    ]
    return sorted(diseases, key=lambda d: d["name"])


async def upsert_diseases(diseases: List[Dict[str, Any]]) -> Dict[str, int]:
    raise RuntimeError("The disease list belongs to the Held app in this database; there is nothing to seed.")


async def ping_database() -> bool:
    pool = await get_db_pool()
    if not pool:
        return False
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return True


async def get_database_overview() -> Dict[str, Any]:
    pool = await get_db_pool()
    if not pool:
        return {"connected": False, "counts": {"users": 0, "diseases": 0, "sessions": 0, "messages": 0}}
    async with pool.acquire() as conn:
        counts = await conn.fetchrow(
            'SELECT (SELECT count(*) FROM "User") AS users_count, (SELECT count(*) FROM "ChatSession") AS sessions_count, '
            '(SELECT count(*) FROM "ChatMessage") AS messages_count'
        )
    return {
        "connected": True,
        "counts": {
            "users_count": int(counts["users_count"] or 0),
            "diseases_count": None,  # not readable by this database user
            "sessions_count": int(counts["sessions_count"] or 0),
            "messages_count": int(counts["messages_count"] or 0),
        },
    }
