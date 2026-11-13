"""Database access for the Held app's PostgreSQL database (Prisma-managed).

The service's database user may read and write "ChatSession" and "ChatMessage", only read
"User" and "GriefNote", and cannot create tables or delete rows. So:

- Chats go into "ChatSession" / "ChatMessage" with the app's column names. Ids are UUID text
  made here, and timestamps are written in UTC (the columns have no time zone, and the server's
  own clock is not UTC).
- The rolling summary, its anchor and the message count have their own "ChatSession" columns
  ("sessionSummary", "lastSummarizedMessageId", "totalMessageCount"). The user memory (a short
  summary of the user's other chats) has none, so it lives in the chat's jsonb metadata under the
  reserved key "memory", which clients never see, and is kept on the user's newest chat.
- Deleting a chat raises PermissionError: the database does not allow it.
- Users are created by the Held app; this service only looks them up. The disease list comes
  from Held's API, because this user cannot read "Disease".
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import asyncpg

from backend.db.connection import get_db_pool
from backend.db.recall import recalled_chat, recalled_message, search_terms

logger = logging.getLogger("held_ai.repository")

NOW = "(now() AT TIME ZONE 'UTC')"  # the columns hold UTC
MEMORY_KEY = "memory"  # the agent's own data inside "ChatSession".metadata (the user memory)
SESSION_COLUMNS = """
    s.id, s."userId", s.title, s.status::text AS status, s.metadata, s."createdAt", s."updatedAt",
    s."sessionSummary", s."lastSummarizedMessageId", s."totalMessageCount"
"""

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
) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]]:
    """The session, its latest `recent` messages oldest first, the user memory, and the profile
    kept with this chat (None until it has one), in one query. The user memory is the one kept on
    the user's newest chat that has one."""
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
                    ORDER BY o."createdAt" DESC LIMIT 1) AS user_memory,
                   s.metadata -> '{MEMORY_KEY}' -> 'profile' AS profile
            FROM "ChatSession" s
            WHERE s.id = $1 AND ($2::text IS NULL OR s."userId" = $2)
            """,
            key, user_id, recent,
        )
    if not row:
        return None
    return _session_dict(row), _memory_messages(row["recent"]), row["user_memory"], _json(row["profile"])


async def save_chat_profile(session_id: str, profile: Dict[str, Any]) -> None:
    """Keep the user's mode fields with the chat (metadata -> 'memory', never shown to clients):
    read once when the chat starts, then reused by its later messages."""
    pool = await get_db_pool()
    key = _key(session_id)
    if not pool or not key:
        return
    merge = _set_memory("'profile', $2::jsonb")
    async with pool.acquire() as conn:
        await conn.execute(f'UPDATE "ChatSession" SET metadata = {merge} WHERE id = $1', key, json.dumps(profile))


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
    reply_metadata: Optional[Dict[str, Any]] = None, prompt_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Both messages, the chat's updatedAt and (for a new chat) its title, in one statement. The
    columns keep milliseconds, so the reply is stamped one millisecond after the prompt.
    reply_metadata (e.g. the reply's token use) goes into the reply's metadata, prompt_metadata
    (e.g. isNote) into the user's message's."""
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
                SELECT $2, $1, 'user', $4, COALESCE($8::jsonb, '{{}}'::jsonb), t.at FROM t
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
            json.dumps(prompt_metadata) if prompt_metadata else None,
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


async def get_user_memory(user_id: str) -> Optional[str]:
    """The user memory (a short summary of their recent chats), kept on their newest chat that has one."""
    pool = await get_db_pool()
    if not pool or not user_id:
        return None
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"""SELECT metadata -> '{MEMORY_KEY}' ->> 'user_memory' FROM "ChatSession"
                WHERE "userId" = $1 AND metadata -> '{MEMORY_KEY}' ? 'user_memory'
                ORDER BY "updatedAt" DESC LIMIT 1""",
            user_id.strip(),
        )


async def search_past_conversations(
    user_id: str, query: str, exclude_session_id: Optional[str] = None, limit: int = 5, days: int = 365,
) -> List[Dict[str, Any]]:
    """What the user's other chats (of the last `days`) said about query, best match first and
    newest on a tie: each message with its chat's title and date, and, for the user's message,
    the reply that followed. Both sides are searched, so a plan the assistant suggested can be
    recalled. With no searchable words, the latest three chats instead."""
    pool = await get_db_pool()
    if not pool or not user_id:
        return []
    terms = search_terms(query)
    async with pool.acquire() as conn:
        if terms:
            rows = await conn.fetch(
                f"""
                WITH mine AS (
                    SELECT s.title, m.role, m.content, m."createdAt" AS at,
                           lead(m.role) OVER w AS next_role, lead(m.content) OVER w AS next_content
                    FROM "ChatMessage" m JOIN "ChatSession" s ON s.id = m."sessionId"
                    WHERE s."userId" = $1 AND ($2::text IS NULL OR s.id <> $2)
                      AND m."createdAt" > {NOW} - make_interval(days => $5)
                    WINDOW w AS (PARTITION BY m."sessionId" ORDER BY m."createdAt", m.id)
                ), q AS (SELECT to_tsquery('english', $3) AS query)
                SELECT title, role, left(content, 500) AS content, at,
                       CASE WHEN role = 'user' AND next_role = 'assistant' THEN left(next_content, 500) END AS reply
                FROM mine, q
                WHERE to_tsvector('english', content) @@ q.query
                ORDER BY ts_rank(to_tsvector('english', content), q.query) DESC, at DESC
                LIMIT $4
                """,
                user_id.strip(), _key(exclude_session_id), terms, limit, days,
            )
            return [recalled_message(r["title"], _iso(r["at"]), r["role"], r["content"], r["reply"]) for r in rows]
        rows = await conn.fetch(
            """
            SELECT s.title, s."updatedAt" AS at, s."sessionSummary" AS summary,
                   (SELECT json_agg(json_build_object('role', m.role, 'content', left(m.content, 300))
                                    ORDER BY m."createdAt", m.id)
                    FROM (SELECT role, content, "createdAt", id FROM "ChatMessage"
                          WHERE "sessionId" = s.id ORDER BY "createdAt" DESC, id DESC LIMIT 4) m) AS recent
            FROM "ChatSession" s
            WHERE s."userId" = $1 AND ($2::text IS NULL OR s.id <> $2)
              AND EXISTS (SELECT 1 FROM "ChatMessage" m WHERE m."sessionId" = s.id)
            ORDER BY s."updatedAt" DESC
            LIMIT 3
            """,
            user_id.strip(), _key(exclude_session_id),
        )
    return [recalled_chat(r["title"], _iso(r["at"]), r["summary"], _json(r["recent"])) for r in rows]


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
                   s.metadata -> 'memory' ->> 'safety_raised_until' AS safety_raised_until,
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
            "safety_raised_until": r["safety_raised_until"],
        }
        for r in rows
    ]


async def mark_safety_check_in(session_id: str, raised_until: str) -> None:
    """Store on this chat (in its metadata, never shown to clients) that its check-in covered
    the user's earlier concerns up to raised_until."""
    pool = await get_db_pool()
    key = _key(session_id)
    if not pool or not key:
        return
    merge = _set_memory("'safety_raised_until', $2::text")
    async with pool.acquire() as conn:
        await conn.execute(f'UPDATE "ChatSession" SET metadata = {merge} WHERE id = $1', key, raised_until)


# ==============================================================================
# Users (read only: the Held app creates them)
# ==============================================================================


async def get_user_profile(user_id: str) -> Optional[Dict[str, Any]]:
    """The user's onboarding answers, shaped like Held's GET /auth/me: role, relation and the
    conditions they chose ("diseases", from "UserDisease" and "Disease"). These pick the reply
    mode. Raises PermissionError while this service may not read the disease tables."""
    pool = await get_db_pool()
    if not pool or not isinstance(user_id, str) or not user_id.strip():
        return None
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                SELECT u.id, u.role::text AS role, u.relation::text AS relation, u."relationOther",
                       u."onboardingComplete",
                       COALESCE((SELECT json_agg(json_build_object('id', d.id, 'name', d.name) ORDER BY d.name)
                                 FROM "UserDisease" ud JOIN "Disease" d ON d.id = ud."diseaseId"
                                 WHERE ud."userId" = u.id), '[]'::json) AS diseases
                FROM "User" u
                WHERE u.id = $1
                """,
                user_id.strip(),
            )
        except asyncpg.InsufficientPrivilegeError as exc:
            raise PermissionError("The disease tables cannot be read on this database.") from exc
    if not row:
        return None
    return {
        "id": row["id"], "role": row["role"], "relation": row["relation"],
        "relationOther": row["relationOther"], "onboardingComplete": row["onboardingComplete"],
        "diseases": _json(row["diseases"]) or [],
    }


async def get_auth_user(user_id: str) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT id, role::text AS role FROM "User" WHERE id = $1', user_id.strip())
    return dict(row) if row else None


async def ping_database() -> bool:
    pool = await get_db_pool()
    if not pool:
        return False
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return True
