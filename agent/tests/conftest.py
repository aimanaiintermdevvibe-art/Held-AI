from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import jwt
import pytest

from backend.core.config import get_settings
from backend.db import repository

TEST_SECRET = "test-secret"


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    """Keep tests off the configured databases (Supabase or the Held development database);
    tests use FakeRepo instead, and the default "agent" table layout whatever .env says."""
    monkeypatch.setattr(get_settings(), "database_schema", "agent")
    monkeypatch.setattr("backend.db.repository.get_db_pool", AsyncMock(return_value=None))
    monkeypatch.setattr("backend.db.held_schema.get_db_pool", AsyncMock(return_value=None))
    monkeypatch.setattr("backend.db.documents.get_db_pool", AsyncMock(return_value=None))
    # Chat tests run without uploaded documents or grief notes unless a test provides some.
    # monkeypatch.setattr("backend.api.routers.agentic.excerpts_for_chat", AsyncMock(return_value=[]))  # RAG disabled
    monkeypatch.setattr("backend.api.routers.agentic.notes_for_chat", AsyncMock(return_value=[]))
    monkeypatch.setattr("backend.db.grief_notes.get_db_pool", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def no_real_gemini(monkeypatch):
    """Background jobs (summary, user memory, titles) use the router's own Gemini client, which
    holds the real key from .env; in tests it fails like an unreachable provider. A test that
    needs it answers patches generate_text itself."""
    from backend.api.routers import agentic

    monkeypatch.setattr(agentic.gemini_client, "generate_text", AsyncMock(side_effect=RuntimeError("no Gemini in tests")))
    monkeypatch.setattr(agentic.gemini_client, "generate_structured", AsyncMock(side_effect=RuntimeError("no Gemini in tests")))


@pytest.fixture(autouse=True, scope="session")
def no_real_ingestion_log():
    """Keep test runs out of logs/document_ingestion.log."""
    import logging

    from backend.rag.ingestion import logger

    file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    for handler in file_handlers:
        logger.removeHandler(handler)
    yield
    for handler in file_handlers:
        logger.addHandler(handler)


@pytest.fixture(autouse=True)
def no_real_held_api(monkeypatch, tmp_path):
    """Keep tests off the Held API and the project's real users/ account files."""
    monkeypatch.setattr("backend.api.routers.agentic.fetch_user_profile", AsyncMock(return_value={}))
    monkeypatch.setattr("backend.core.auth.held_me", AsyncMock(return_value=None))  # Held rejects
    monkeypatch.setattr("backend.core.auth._confirmed_tokens", {})  # no confirmations carried between tests
    users_dir = tmp_path / "users"
    users_dir.mkdir()
    monkeypatch.setattr("backend.core.user_accounts.USERS_DIR", users_dir)


class FakeRepo:
    """In-memory stand-in for backend.db.repository with the same behaviour."""

    def __init__(self):
        self.users = {"usr_1": "USER", "usr_2": "CAREGIVER", "adm_1": "ADMIN"}
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.user_memory: dict[str, str] = {}
        self.created_from_profiles: list[dict] = []
        self.ops: list[str] = []  # chat-related database calls, in order

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def get_auth_user(self, user_id):
        role = self.users.get(user_id)
        return {"id": user_id, "role": role} if role else None

    async def create_user_from_profile(self, profile):
        self.created_from_profiles.append(profile)
        self.users.setdefault(profile["id"], "CAREGIVER" if profile.get("role") == "CAREGIVER" else "USER")
        return await self.get_auth_user(profile["id"])

    async def create_chat_session(self, user_id=None, title="New Conversation", metadata=None):
        self.ops.append("create_chat_session")
        if user_id not in self.users:
            return None
        sid = str(uuid4())
        self.sessions[sid] = {
            "id": sid, "user_id": user_id, "title": title, "status": "active",
            "metadata": metadata or {}, "session_summary": None,
            "last_summarized_message_id": None, "total_message_count": 0,
            "created_at": self._now(), "updated_at": self._now(),
        }
        self.messages[sid] = []
        return dict(self.sessions[sid])

    def _session(self, session_id, user_id=None):
        session = self.sessions.get(session_id)
        if not session or (user_id is not None and session["user_id"] != user_id):
            return None
        return dict(session)

    @staticmethod
    def _brief(message):
        return {k: message[k] for k in ("id", "role", "content", "created_at")}

    async def get_chat_session(self, session_id, user_id=None):
        self.ops.append("get_chat_session")
        return self._session(session_id, user_id)

    async def get_session_messages(self, session_id):
        self.ops.append("get_session_messages")
        return [dict(m) for m in self.messages.get(session_id, [])]

    async def open_chat(self, session_id, user_id=None, recent=20):
        self.ops.append("open_chat")
        session = self._session(session_id, user_id)
        if not session:
            return None
        latest = [self._brief(m) for m in self.messages[session_id][-recent:]]
        return session, latest, self.user_memory.get(session["user_id"])

    async def get_messages_after(self, session_id, anchor_id, limit=200):
        self.ops.append("get_messages_after")
        stored = self.messages.get(session_id, [])
        ids = [m["id"] for m in stored]
        start = ids.index(anchor_id) + 1 if anchor_id in ids else 0
        return [self._brief(m) for m in stored[start:start + limit]]

    async def save_turn(self, session_id, prompt, response, title=None):
        self.ops.append("save_turn")
        session = self.sessions.get(session_id)
        if not session:
            return None
        saved = [
            {"id": str(uuid4()), "session_id": session_id, "role": role, "content": content,
             "metadata": {}, "created_at": self._now()}
            for role, content in (("user", prompt), ("assistant", response))
        ]
        self.messages[session_id].extend(saved)
        session["total_message_count"] += 2
        session["updated_at"] = self._now()
        if title is not None:
            session["title"] = title
        return {
            "messages": [self._brief(m) for m in saved], "total_message_count": session["total_message_count"],
            "title": session["title"], "updated_at": session["updated_at"],
        }

    async def save_session_summary(self, session_id, session_summary, last_summarized_message_id,
                                   previous_anchor=None):
        self.ops.append("save_session_summary")
        session = self.sessions.get(session_id)
        if not session or session["last_summarized_message_id"] != previous_anchor:
            return False
        session["session_summary"] = session_summary
        session["last_summarized_message_id"] = last_summarized_message_id
        return True

    async def save_user_memory(self, user_id, summary):
        self.ops.append("save_user_memory")
        self.user_memory[user_id] = summary

    async def get_previous_conversations(self, user_id, exclude_session_id=None, limit=5, **_):
        self.ops.append("get_previous_conversations")
        found = [
            s for sid, s in self.sessions.items()
            if s["user_id"] == user_id and sid != exclude_session_id and self.messages[sid]
        ]
        found.sort(key=lambda s: s["updated_at"], reverse=True)
        recaps = []
        for s in found[:limit]:
            user_messages = [
                {"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
                for m in self.messages[s["id"]] if m["role"] == "user"
            ][-50:]
            recaps.append({
                "title": s["title"], "last_active": s["updated_at"], "summary": s["session_summary"],
                "recent_messages": user_messages[-6:], "user_messages": user_messages,
            })
        return recaps

    async def list_chat_sessions(self, user_id=None):
        return [
            {**s, "message_count": s["total_message_count"]}
            for s in self.sessions.values() if user_id is None or s["user_id"] == user_id
        ]

    async def delete_chat_session(self, session_id, user_id=None):
        self.ops.append("delete_chat_session")
        if not self._session(session_id, user_id):
            return False
        self.sessions.pop(session_id)
        self.messages.pop(session_id)
        return True

    async def ping_database(self):
        return True

    async def get_database_overview(self):
        return {"connected": True, "counts": {
            "users_count": len(self.users), "sessions_count": len(self.sessions),
            "messages_count": sum(len(m) for m in self.messages.values()),
        }}

    async def list_users(self, limit=50):
        return [{"id": uid, "role": role} for uid, role in self.users.items()][:limit]

    async def get_user_by_id(self, user_id):
        return {"id": user_id, "role": self.users[user_id]} if user_id in self.users else None


@pytest.fixture
def fake_repo(monkeypatch):
    repo = FakeRepo()
    for name in (
        "get_auth_user", "create_chat_session", "get_chat_session", "get_session_messages",
        "open_chat", "get_messages_after", "save_turn", "save_session_summary", "save_user_memory",
        "get_previous_conversations",
        "list_chat_sessions", "delete_chat_session", "ping_database", "get_database_overview",
        "list_users", "get_user_by_id", "create_user_from_profile",
    ):
        monkeypatch.setattr(repository, name, getattr(repo, name))
    return repo


@pytest.fixture
def auth(monkeypatch):
    """Return a factory for Authorization headers signed like Held access tokens."""
    monkeypatch.setattr(get_settings(), "jwt_secret", TEST_SECRET)

    def headers(user_id="usr_1"):
        return {"Authorization": f"Bearer {jwt.encode({'sub': user_id}, TEST_SECRET, algorithm='HS256')}"}

    return headers