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
    """Keep tests off the configured database; tests use FakeRepo instead."""
    monkeypatch.setattr("backend.db.repository.get_db_pool", AsyncMock(return_value=None))
    # Chat tests run without grief notes unless a test provides some.
    monkeypatch.setattr("backend.db.grief_notes.get_db_pool", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def no_real_gemini(monkeypatch):
    """Background jobs (summary, user memory, titles) use the router's own Gemini client, which
    holds the real key from .env; in tests it fails like an unreachable provider. A test that
    needs it answers patches generate_text itself."""
    from backend.api.routers import agentic

    monkeypatch.setattr(agentic.gemini_client, "generate_text", AsyncMock(side_effect=RuntimeError("no Gemini in tests")))
    monkeypatch.setattr(agentic.gemini_client, "generate_structured", AsyncMock(side_effect=RuntimeError("no Gemini in tests")))
    monkeypatch.setattr(agentic.gemini_client, "generate_text_with_tools", AsyncMock(side_effect=RuntimeError("no Gemini in tests")))


@pytest.fixture(autouse=True)
def no_real_held_api(monkeypatch):
    """Keep tests off the Held API."""
    monkeypatch.setattr("backend.api.routers.agentic.fetch_user_profile", AsyncMock(return_value={}))
    monkeypatch.setattr("backend.core.auth.held_me", AsyncMock(return_value=None))  # Held rejects
    monkeypatch.setattr("backend.core.auth._confirmed_tokens", {})  # no confirmations carried between tests


class FakeRepo:
    """In-memory stand-in for backend.db.repository with the same behaviour."""

    def __init__(self):
        self.users = {"usr_1": "USER", "usr_2": "CAREGIVER", "adm_1": "ADMIN"}
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.user_memory: dict[str, str] = {}
        self.check_ins: dict[str, str] = {}
        self.chat_profiles: dict[str, dict] = {}  # session id -> profile kept with the chat  # session id -> newest concern its check-in covered
        self.profiles: dict[str, dict] = {}  # onboarding answers by user id; none means "use Held's API"
        self.ops: list[str] = []  # chat-related database calls, in order

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def get_auth_user(self, user_id):
        role = self.users.get(user_id)
        return {"id": user_id, "role": role} if role else None

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
        return session, latest, self.user_memory.get(session["user_id"]), self.chat_profiles.get(session_id)

    async def save_chat_profile(self, session_id, profile):
        self.ops.append("save_chat_profile")
        self.chat_profiles[session_id] = dict(profile)

    async def get_messages_after(self, session_id, anchor_id, limit=200):
        self.ops.append("get_messages_after")
        stored = self.messages.get(session_id, [])
        ids = [m["id"] for m in stored]
        start = ids.index(anchor_id) + 1 if anchor_id in ids else 0
        return [self._brief(m) for m in stored[start:start + limit]]

    async def save_turn(self, session_id, prompt, response, title=None, reply_metadata=None, prompt_metadata=None):
        self.ops.append("save_turn")
        session = self.sessions.get(session_id)
        if not session:
            return None
        saved = [
            {"id": str(uuid4()), "session_id": session_id, "role": role, "content": content,
             "metadata": metadata, "created_at": self._now()}
            for role, content, metadata in (("user", prompt, dict(prompt_metadata or {})), ("assistant", response, dict(reply_metadata or {})))
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
                "safety_raised_until": self.check_ins.get(s["id"]),
            })
        return recaps

    async def get_user_memory(self, user_id):
        self.ops.append("get_user_memory")
        return self.user_memory.get(user_id)

    async def search_past_conversations(self, user_id, query, exclude_session_id=None, limit=5, **_):
        from backend.db.recall import recalled_chat, recalled_message, search_terms

        self.ops.append("search_past_conversations")
        words = [w for w in search_terms(query).split(" | ") if w]
        chats = [s for sid, s in self.sessions.items()
                 if s["user_id"] == user_id and sid != exclude_session_id and self.messages[sid]]
        chats.sort(key=lambda s: s["updated_at"], reverse=True)
        if not words:
            return [recalled_chat(s["title"], s["updated_at"], s["session_summary"], self.messages[s["id"]][-4:])
                    for s in chats[:3]]
        found = []
        for s in chats:
            msgs = self.messages[s["id"]]
            for i, m in enumerate(msgs):
                if any(w in m["content"].lower() for w in words):
                    reply = msgs[i + 1]["content"] if m["role"] == "user" and i + 1 < len(msgs) else None
                    found.append(recalled_message(s["title"], m["created_at"], m["role"], m["content"], reply))
        return found[:limit]

    async def mark_safety_check_in(self, session_id, raised_until):
        self.ops.append("mark_safety_check_in")
        self.check_ins[session_id] = raised_until

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

    async def get_user_profile(self, user_id):
        profile = self.profiles.get(user_id)
        if isinstance(profile, Exception):
            raise profile
        return dict(profile) if profile else None



@pytest.fixture
def fake_repo(monkeypatch):
    repo = FakeRepo()
    for name in (
        "get_auth_user", "create_chat_session", "get_chat_session", "get_session_messages",
        "open_chat", "get_messages_after", "save_turn", "save_session_summary", "save_user_memory",
        "get_previous_conversations", "mark_safety_check_in", "get_user_memory", "search_past_conversations", "save_chat_profile",
        "list_chat_sessions", "delete_chat_session", "ping_database", "get_user_profile",
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