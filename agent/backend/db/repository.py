import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
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


def _session_dict(row: Any) -> Dict[str, Any]:
    anchor = row["last_summarized_message_id"]
    return {
        "id": str(row["id"]),
        "user_id": row["user_id"],
        "title": row["title"],
        "status": row["status"],
        "metadata": _json(row["metadata"]),
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
    if "total_message_count" in row.keys():
        message["total_message_count"] = int(row["total_message_count"] or 0)
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


async def update_chat_session(
    session_id: str,
    title: Optional[str] = None,
    session_summary: Optional[str] = None,
    last_summarized_message_id: Optional[str] = None,
) -> None:
    """Set the AI-chosen title and/or the rolling summary with its anchor message."""
    pool = await get_db_pool()
    session_key = _uuid(session_id)
    if not pool or not session_key:
        return

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE public.chat_sessions
            SET title = COALESCE($2, title),
                session_summary = COALESCE($3, session_summary),
                last_summarized_message_id = COALESCE($4, last_summarized_message_id),
                updated_at = NOW()
            WHERE id = $1;
            """,
            session_key,
            title,
            session_summary,
            _uuid(last_summarized_message_id),
        )


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


async def save_chat_message(
    session_id: str,
    role: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Insert a message and bump its session's total_message_count in one statement."""
    pool = await get_db_pool()
    session_key = _uuid(session_id)
    if not pool or not session_key:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH m AS (
                INSERT INTO public.chat_messages (session_id, role, content, metadata)
                VALUES ($1, $2, $3, COALESCE($4::jsonb, '{}'::jsonb))
                RETURNING id, session_id, role, content, metadata, created_at
            ), s AS (
                UPDATE public.chat_sessions
                SET total_message_count = total_message_count + 1, updated_at = NOW()
                WHERE id = $1
                RETURNING total_message_count
            )
            SELECT m.*, s.total_message_count FROM m, s;
            """,
            session_key,
            role,
            content,
            json.dumps(metadata) if metadata is not None else None,
        )
        return _message_dict(row) if row else None


async def get_previous_conversations(
    user_id: str,
    exclude_session_id: Optional[str] = None,
    limit: int = 10,
    recent_messages: int = 6,
    content_chars: int = 600,
) -> List[Dict[str, Any]]:
    """Return the user's most recent other conversations for cross-session memory.

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
                   (SELECT json_agg(json_build_object('role', m.role, 'content', left(m.content, $5))
                                    ORDER BY m.created_at, m.id)
                    FROM (SELECT role, content, created_at, id FROM public.chat_messages
                          WHERE session_id = s.id AND role = 'user'
                          ORDER BY created_at DESC, id DESC LIMIT $4) m) AS recent,
                   (SELECT json_agg(json_build_object('role', m.role, 'content', left(m.content, $5))
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
