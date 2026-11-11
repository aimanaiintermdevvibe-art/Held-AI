import functools
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from backend.db.connection import get_db_pool

logger = logging.getLogger("held_ai.repository")


# ==============================================================================
# Chat Session & Message Operations
#
# Backed by public.chat_sessions / public.chat_messages (uuid ids). Every value
# lives in its own column: the rolling summary in session_summary, its anchor in
# last_summarized_message_id, and the running count in total_message_count.
# metadata holds only client-supplied data.
# ==============================================================================

SESSION_COLUMNS = """
    s.id, s.user_id, s.title, s.status, s.metadata, s.session_summary,
    s.last_summarized_message_id, s.total_message_count, s.created_at, s.updated_at
"""


def _uuid(value: Optional[str]) -> Optional[UUID]:
    """Parse a client-supplied id; malformed ids are treated as not found."""
    if not value or not isinstance(value, str):
        return None
    try:
        return UUID(value.strip())
    except ValueError:
        return None


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value else None


def _client_metadata(value: Any) -> Any:
    """A chat's metadata without the agent's own "memory" key, which clients never see."""
    metadata = _json(value)
    return {k: v for k, v in metadata.items() if k != "memory"} if isinstance(metadata, dict) else metadata


def _session_dict(row: Any) -> Dict[str, Any]:
    anchor = row["last_summarized_message_id"]
    return {
        "id": str(row["id"]),
        "user_id": row["user_id"],
        "title": row["title"],
        "status": row["status"],
        "metadata": _client_metadata(row["metadata"]),
        "session_summary": row["session_summary"],
        "last_summarized_message_id": str(anchor) if anchor else None,
        "total_message_count": int(row["total_message_count"] or 0),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _message_dict(row: Any) -> Dict[str, Any]:
    message = {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "role": row["role"],
        "content": row["content"],
        "metadata": _json(row["metadata"]),
        "created_at": _iso(row["created_at"]),
    }
    return message


async def list_chat_sessions(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List chat sessions with message counts and timestamps."""
    pool = await get_db_pool()
    if not pool:
        return []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {SESSION_COLUMNS},
                   (SELECT MAX(m.created_at) FROM public.chat_messages m
                    WHERE m.session_id = s.id) AS last_message_at
            FROM public.chat_sessions s
            WHERE ($1::text IS NULL OR s.user_id = $1)
            ORDER BY s.updated_at DESC NULLS LAST, s.created_at DESC;
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
    user_id: Optional[str] = None,
    title: str = "New Conversation",
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Create a chat session owned by an existing public."User" row."""
    pool = await get_db_pool()
    if not pool:
        return None
    if not user_id or not isinstance(user_id, str) or not user_id.strip():
        logger.warning("Refusing to create chat session for a missing user id.")
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO public.chat_sessions AS s (user_id, title, status, metadata)
            SELECT u.id, $2, 'active', COALESCE($3::jsonb, '{{}}'::jsonb)
            FROM public."User" u WHERE u.id = $1
            RETURNING {SESSION_COLUMNS};
            """,
            user_id.strip(),
            title,
            json.dumps(metadata) if metadata is not None else None,
        )
        if not row:
            logger.warning("Refusing to create chat session for an unknown user id.")
            return None
        return _session_dict(row)


async def get_chat_session(session_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Get a chat session (scoped to its owner unless user_id is None)."""
    pool = await get_db_pool()
    session_key = _uuid(session_id)
    if not pool or not session_key:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {SESSION_COLUMNS} FROM public.chat_sessions s
            WHERE s.id = $1 AND ($2::text IS NULL OR s.user_id = $2);
            """,
            session_key,
            user_id,
        )
        return _session_dict(row) if row else None


async def save_session_summary(
    session_id: str,
    session_summary: str,
    last_summarized_message_id: str,
    previous_anchor: Optional[str] = None,
) -> bool:
    """Store a new rolling summary and its anchor message, unless another job already moved
    the anchor on from previous_anchor. Returns whether it was stored."""
    pool = await get_db_pool()
    session_key = _uuid(session_id)
    if not pool or not session_key:
        return False

    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE public.chat_sessions
            SET session_summary = $2, last_summarized_message_id = $3
            WHERE id = $1 AND last_summarized_message_id IS NOT DISTINCT FROM $4;
            """,
            session_key,
            session_summary,
            _uuid(last_summarized_message_id),
            _uuid(previous_anchor),
        )
        return result == "UPDATE 1"


async def delete_chat_session(session_id: str, user_id: Optional[str] = None) -> bool:
    """Delete a chat session and its cascading messages."""
    pool = await get_db_pool()
    session_key = _uuid(session_id)
    if not pool or not session_key:
        return False

    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM public.chat_sessions WHERE id = $1 AND ($2::text IS NULL OR user_id = $2);",
            session_key,
            user_id,
        )
        # res looks like "DELETE 1"
        return res.startswith("DELETE") and not res.endswith(" 0")


async def get_session_messages(session_id: str) -> List[Dict[str, Any]]:
    """Retrieve all messages for a session in chronological order."""
    pool = await get_db_pool()
    session_key = _uuid(session_id)
    if not pool or not session_key:
        return []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, session_id, role, content, metadata, created_at
            FROM public.chat_messages
            WHERE session_id = $1
            ORDER BY created_at ASC, id ASC;
            """,
            session_key,
        )
        return [_message_dict(r) for r in rows]


def _memory_messages(value: Any) -> List[Dict[str, Any]]:
    """json_agg'd messages (id, role, content, created_at), oldest first."""
    return [
        {"id": str(m["id"]), "role": m["role"], "content": m["content"], "created_at": m.get("created_at")}
        for m in _json(value) or []
    ]


async def open_chat(
    session_id: str, user_id: Optional[str] = None, recent: int = 20,
) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]], Optional[str]]]:
    """Everything a reply in an existing chat needs, in one query: the session (scoped to its
    owner unless user_id is None), its latest `recent` messages oldest first, and the owner's
    long-term memory (public.user_memory, migration 006). None when the session is not found."""
    pool = await get_db_pool()
    session_key = _uuid(session_id)
    if not pool or not session_key:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {SESSION_COLUMNS},
                   (SELECT json_agg(json_build_object('id', m.id, 'role', m.role, 'content', m.content,
                                                      'created_at', m.created_at)
                                    ORDER BY m.created_at, m.id)
                    FROM (SELECT id, role, content, created_at FROM public.chat_messages
                          WHERE session_id = s.id
                          ORDER BY created_at DESC, id DESC LIMIT $3) m) AS recent,
                   (SELECT um.summary FROM public.user_memory um WHERE um.user_id = s.user_id) AS user_memory
            FROM public.chat_sessions s
            WHERE s.id = $1 AND ($2::text IS NULL OR s.user_id = $2);
            """,
            session_key,
            user_id,
            recent,
        )
        if not row:
            return None
        return _session_dict(row), _memory_messages(row["recent"]), row["user_memory"]


async def get_messages_after(
    session_id: str, anchor_id: Optional[str], limit: int = 200,
) -> List[Dict[str, Any]]:
    """Up to `limit` messages after the anchor message (from the start when there is none),
    oldest first. Only needed when a summary has fallen behind the messages a turn loaded."""
    pool = await get_db_pool()
    session_key = _uuid(session_id)
    if not pool or not session_key:
        return []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.id, m.role, m.content, m.created_at
            FROM public.chat_messages m
            WHERE m.session_id = $1
              AND ($2::uuid IS NULL OR (m.created_at, m.id) > (
                    SELECT a.created_at, a.id FROM public.chat_messages a WHERE a.id = $2))
            ORDER BY m.created_at, m.id
            LIMIT $3;
            """,
            session_key,
            _uuid(anchor_id),
            limit,
        )
        return [
            {"id": str(r["id"]), "role": r["role"], "content": r["content"], "created_at": _iso(r["created_at"])}
            for r in rows
        ]


async def save_turn(
    session_id: str, prompt: str, response: str, title: Optional[str] = None,
    reply_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Store a turn in one statement: the user's message and the reply, the session's message
    count, and (for a new chat) its title. Returns the two stored messages with the session's
    new total_message_count, title and updated_at. reply_metadata (e.g. the reply's token use)
    goes into the reply's metadata."""
    pool = await get_db_pool()
    session_key = _uuid(session_id)
    if not pool or not session_key:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH m AS (
                -- One statement shares one NOW(); the reply is stamped just after the prompt
                -- so the two always sort in order.
                INSERT INTO public.chat_messages (session_id, role, content, metadata, created_at)
                VALUES ($1, 'user', $2, '{}'::jsonb, NOW()),
                       ($1, 'assistant', $3, COALESCE($5::jsonb, '{}'::jsonb), NOW() + interval '1 microsecond')
                RETURNING id, role, content, created_at
            ), s AS (
                UPDATE public.chat_sessions
                SET total_message_count = total_message_count + 2, updated_at = NOW(),
                    title = COALESCE($4, title)
                WHERE id = $1
                RETURNING total_message_count, title, updated_at
            )
            SELECT (SELECT json_agg(json_build_object('id', m.id, 'role', m.role, 'content', m.content,
                                                      'created_at', m.created_at)
                                    ORDER BY m.created_at) FROM m) AS messages,
                   s.total_message_count, s.title, s.updated_at
            FROM s;
            """,
            session_key,
            prompt,
            response,
            title,
            json.dumps(reply_metadata) if reply_metadata else None,
        )
        if not row:
            return None
        return {
            "messages": _memory_messages(row["messages"]),
            "total_message_count": int(row["total_message_count"] or 0),
            "title": row["title"],
            "updated_at": _iso(row["updated_at"]),
        }


async def save_user_memory(user_id: str, summary: str) -> None:
    """Store the user's long-term memory (public.user_memory, migration 006)."""
    pool = await get_db_pool()
    if not pool or not user_id:
        return

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.user_memory (user_id, summary, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET summary = EXCLUDED.summary, updated_at = NOW();
            """,
            user_id,
            summary,
        )


async def get_previous_conversations(
    user_id: str,
    exclude_session_id: Optional[str] = None,
    limit: int = 10,
    recent_messages: int = 6,
    content_chars: int = 600,
) -> List[Dict[str, Any]]:
    """Return the user's most recent other conversations for cross-session memory. Read for
    a new chat's first reply only; later replies use the shorter user memory instead.

    Each item carries the session's rolling summary (when one exists), the user's
    last few messages there, and up to 50 of their messages in `user_messages` (so
    memory can find an earlier safety concern), truncated, oldest first. Assistant
    replies are left out so the model never treats its own earlier wording as facts.
    """
    pool = await get_db_pool()
    if not pool or not user_id:
        return []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.title, s.session_summary, s.updated_at,
                   s.metadata -> 'memory' ->> 'safety_raised_until' AS safety_raised_until,
                   (SELECT json_agg(json_build_object('role', m.role, 'content', left(m.content, $5))
                                    ORDER BY m.created_at, m.id)
                    FROM (SELECT role, content, created_at, id FROM public.chat_messages
                          WHERE session_id = s.id AND role = 'user'
                          ORDER BY created_at DESC, id DESC LIMIT $4) m) AS recent,
                   (SELECT json_agg(json_build_object('role', m.role, 'content', left(m.content, $5),
                                                      'created_at', m.created_at)
                                    ORDER BY m.created_at, m.id)
                    FROM (SELECT role, content, created_at, id FROM public.chat_messages
                          WHERE session_id = s.id AND role = 'user'
                          ORDER BY created_at DESC, id DESC LIMIT 50) m) AS user_messages
            FROM public.chat_sessions s
            WHERE s.user_id = $1
              AND ($2::uuid IS NULL OR s.id <> $2)
              AND EXISTS (SELECT 1 FROM public.chat_messages m WHERE m.session_id = s.id)
            ORDER BY s.updated_at DESC NULLS LAST
            LIMIT $3;
            """,
            user_id.strip(),
            _uuid(exclude_session_id),
            limit,
            recent_messages,
            content_chars,
        )
        return [
            {
                "title": r["title"],
                "last_active": _iso(r["updated_at"]),
                "summary": r["session_summary"],
                "recent_messages": _json(r["recent"]) or [],
                "user_messages": _json(r["user_messages"]) or [],
                "safety_raised_until": r["safety_raised_until"],
            }
            for r in rows
        ]


# ==============================================================================
# User & Disease Operations
# ==============================================================================

async def list_users(limit: int = 50) -> List[Dict[str, Any]]:
    """List users from public."User"."""
    pool = await get_db_pool()
    if not pool:
        return []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, email, "fullName", username, role, relation, "createdAt"
            FROM public."User"
            ORDER BY "createdAt" DESC
            LIMIT $1;
            """,
            limit,
        )
        return [
            {
                "id": r["id"],
                "email": r["email"],
                "fullName": r["fullName"],
                "username": r["username"],
                "role": r["role"],
                "relation": r["relation"],
                "createdAt": r["createdAt"].isoformat() if r["createdAt"] else None,
            }
            for r in rows
        ]


async def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve user details by ID."""
    pool = await get_db_pool()
    if not pool:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, email, "fullName", username, role, relation, "createdAt" '
            'FROM public."User" WHERE id = $1;',
            user_id.strip(),
        )
        if not row:
            return None
        return dict(row)


async def get_auth_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Look up the id and role used to authenticate a token subject.

    Returns None when the user does not exist; raises RuntimeError when the
    database is unreachable so callers can tell the two apart.
    """
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, role::text AS role FROM public."User" WHERE id = $1;',
            user_id.strip(),
        )
        return dict(row) if row else None


# Held API values -> this database's enums. Held only issues SELF and CAREGIVER, so a
# copied user can never become ADMIN; Held's PARTNER is this schema's SPOUSE.
_ROLE_FROM_HELD = {"SELF": "USER", "CAREGIVER": "CAREGIVER"}
_RELATIONS = {"PARENT", "CHILD", "SIBLING", "FRIEND", "OTHER"}
_AUTH_PROVIDERS = {"EMAIL", "GOOGLE", "APPLE", "FACEBOOK", "GITHUB"}
_OAUTH_PROVIDERS = {"GOOGLE", "APPLE", "FACEBOOK", "GITHUB"}


def _naive_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def user_values_from_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    """public."User" column values for a Held /auth/me profile (no password: Held keeps credentials)."""
    relation = profile.get("relation")
    relation = "SPOUSE" if relation == "PARTNER" else relation if relation in _RELATIONS else None
    provider, provider_id = profile.get("provider"), profile.get("providerId")
    if provider not in _OAUTH_PROVIDERS or not provider_id:
        provider = provider_id = None
    auth_provider = profile.get("authProvider")
    return {
        "id": profile["id"],
        "email": profile["email"],
        "fullName": profile.get("fullName") or profile["email"].split("@")[0],
        "authProvider": auth_provider if auth_provider in _AUTH_PROVIDERS else "EMAIL",
        "provider": provider,
        "providerId": provider_id,
        "emailVerified": bool(profile.get("emailVerified")),
        "onboardingComplete": bool(profile.get("onboardingComplete")),
        "role": _ROLE_FROM_HELD.get(profile.get("role")),
        "dob": _naive_utc(profile.get("dob")),
        "username": profile.get("username") or None,
        "pseudoName": profile.get("pseudoName") or None,
        "relation": relation,
        "relationOther": profile.get("relationOther") if relation == "OTHER" else None,
    }


async def create_user_from_profile(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Add a Held-registered user to public."User" so the agent can store their chats.

    Existing rows are never changed. Returns the auth row (id, role), or None when the
    profile is incomplete or its email already belongs to a different user id.
    Raises RuntimeError when the database is unreachable.
    """
    if not isinstance(profile.get("id"), str) or not isinstance(profile.get("email"), str):
        return None
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")

    values = user_values_from_profile(profile)
    async with pool.acquire() as conn:
        # A username taken by someone else in this database is dropped rather than blocking the user.
        for username in dict.fromkeys((values["username"], None)):
            await conn.execute(
                """
                INSERT INTO public."User" (
                    id, email, password, "fullName", "authProvider", provider, "providerId",
                    "emailVerified", "onboardingComplete", role, dob, username, "pseudoName",
                    relation, "relationOther", "updatedAt"
                )
                VALUES (
                    $1, $2, NULL, $3, $4::text::"AuthProvider", $5::text::"OAuthProvider", $6,
                    $7, $8, $9::text::"UserRole", $10, $11, $12,
                    $13::text::"RelationType", $14, CURRENT_TIMESTAMP
                )
                ON CONFLICT DO NOTHING;
                """,
                values["id"], values["email"], values["fullName"], values["authProvider"],
                values["provider"], values["providerId"], values["emailVerified"],
                values["onboardingComplete"], values["role"], values["dob"], username,
                values["pseudoName"], values["relation"], values["relationOther"],
            )
            row = await conn.fetchrow(
                'SELECT id, role::text AS role FROM public."User" WHERE id = $1;', values["id"],
            )
            if row:
                logger.info("Added Held user %s to public.\"User\".", values["id"])
                return dict(row)

    logger.warning("Could not add Held user %s: the email belongs to a different user id.", values["id"])
    return None


async def list_diseases() -> List[Dict[str, Any]]:
    """The disease catalogue (migration 003, filled by scripts/seed_diseases.py), by name."""
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT id, name, abbreviation, "imageUrl" FROM public."Disease" ORDER BY name;'
        )
        return [dict(r) for r in rows]


async def upsert_diseases(diseases: List[Dict[str, Any]]) -> Dict[str, int]:
    """Insert or update catalogue entries by id; entries not in the list are left alone."""
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = {r["id"]: tuple(r[1:]) for r in await conn.fetch(
                'SELECT id, name, abbreviation, "imageUrl" FROM public."Disease";'
            )}
            await conn.executemany(
                """
                INSERT INTO public."Disease" (id, name, abbreviation, "imageUrl")
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name, abbreviation = EXCLUDED.abbreviation,
                    "imageUrl" = EXCLUDED."imageUrl", "updatedAt" = CURRENT_TIMESTAMP
                WHERE (public."Disease".name, public."Disease".abbreviation, public."Disease"."imageUrl")
                      IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.abbreviation, EXCLUDED."imageUrl");
                """,
                [(d["id"], d["name"], d.get("abbreviation"), d.get("imageUrl")) for d in diseases],
            )
    added = sum(1 for d in diseases if d["id"] not in before)
    changed = sum(
        1 for d in diseases
        if d["id"] in before and before[d["id"]] != (d["name"], d.get("abbreviation"), d.get("imageUrl"))
    )
    return {"added": added, "updated": changed, "unchanged": len(diseases) - added - changed}


async def ping_database() -> bool:
    """Whether the database answers a trivial query."""
    pool = await get_db_pool()
    if not pool:
        return False

    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1;")
    return True


async def get_database_overview() -> Dict[str, Any]:
    """Return counts for tables confirmed in the Held database."""
    pool = await get_db_pool()
    if not pool:
        return {
            "connected": False,
            "counts": {"users": 0, "diseases": 0, "sessions": 0, "messages": 0},
        }

    async with pool.acquire() as conn:
        counts = await conn.fetchrow(
            """
            SELECT 
              (SELECT COUNT(*) FROM public."User") as users_count,
              (SELECT COUNT(*) FROM public."Disease") as diseases_count,
              (SELECT COUNT(*) FROM public.chat_sessions) as sessions_count,
              (SELECT COUNT(*) FROM public.chat_messages) as messages_count;
            """
        )
        return {
            "connected": True,
            "counts": {
                "users_count": int(counts["users_count"] or 0),
                "diseases_count": int(counts["diseases_count"] or 0),
                "sessions_count": int(counts["sessions_count"] or 0),
                "messages_count": int(counts["messages_count"] or 0),
            },
        }


# ==============================================================================
# The Held app's own development database (DATABASE_SCHEMA=held) keeps chats in its
# "ChatSession" / "ChatMessage" tables. There, each function above is served by the
# function of the same name in held_schema.py; with the default "agent" schema
# (Supabase), the functions above run unchanged.
# ==============================================================================

async def mark_safety_check_in(session_id: str, raised_until: str) -> None:
    """Store on this chat (metadata -> 'memory', never shown to clients) that its check-in covered
    the user's earlier concerns up to raised_until."""
    pool = await get_db_pool()
    session_key = _uuid(session_id)
    if not pool or not session_key:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE public.chat_sessions
            SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                'memory', COALESCE(metadata -> 'memory', '{}'::jsonb) || jsonb_build_object('safety_raised_until', $2::text))
            WHERE id = $1
            """,
            session_key, raised_until,
        )


def _schema_aware(function):
    @functools.wraps(function)
    async def run(*args, **kwargs):
        from backend.core.config import get_settings

        if get_settings().database_schema == "held":
            from backend.db import held_schema

            return await getattr(held_schema, function.__name__)(*args, **kwargs)
        return await function(*args, **kwargs)

    return run


def _wrap_schema_aware_functions() -> None:
    from backend.db import held_schema

    for name in held_schema.__all__:
        globals()[name] = _schema_aware(globals()[name])


_wrap_schema_aware_functions()
