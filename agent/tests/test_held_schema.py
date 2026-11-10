"""DATABASE_SCHEMA=held: the Held app's development database, with its own "ChatSession" and
"ChatMessage" tables, read-only users, and no tables of the agent's own."""
import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

from backend.core.config import get_settings
from backend.db import grief_notes as notes_store
from backend.db import held_schema, repository
from backend.grief import service


@pytest.fixture
def held_db(monkeypatch):
    monkeypatch.setattr(get_settings(), "database_schema", "held")


def test_repository_calls_go_to_the_held_tables_only_when_chosen(monkeypatch, held_db):
    opened = AsyncMock(return_value=None)
    monkeypatch.setattr(held_schema, "open_chat", opened)
    asyncio.run(repository.open_chat("chat-1", user_id="usr_1", recent=20))
    opened.assert_awaited_once_with("chat-1", user_id="usr_1", recent=20)

    monkeypatch.setattr(get_settings(), "database_schema", "agent")
    asyncio.run(repository.open_chat("chat-1"))
    assert opened.await_count == 1  # the default (Supabase) layout runs its own query


def test_every_repository_function_has_a_held_version():
    for name in held_schema.__all__:
        assert asyncio.iscoroutinefunction(getattr(held_schema, name)), name
        assert getattr(repository, name).__wrapped__.__module__ == "backend.db.repository"


def test_a_session_uses_the_summary_columns_and_hides_the_user_memory():
    row = {
        "id": "c1", "userId": "usr_1", "title": "Work stress", "status": "ACTIVE",
        "metadata": '{"source": "app", "memory": {"user_memory": "They have a dog."}}',
        "createdAt": datetime(2026, 11, 6, 9, 0), "updatedAt": datetime(2026, 11, 6, 9, 5),
        "sessionSummary": "Facts: ...", "lastSummarizedMessageId": "m9", "totalMessageCount": 12,
    }
    session = held_schema._session_dict(row)
    assert session["metadata"] == {"source": "app"}  # the user memory is never shown to clients
    assert session["session_summary"] == "Facts: ..." and session["last_summarized_message_id"] == "m9"
    assert session["created_at"] == "2026-11-06T09:00:00+00:00"  # stored in UTC
    assert session["total_message_count"] == 12 and session["user_id"] == "usr_1"
    empty = held_schema._session_dict({**row, "metadata": None, "sessionSummary": None, "lastSummarizedMessageId": None})
    assert empty["metadata"] == {} and empty["session_summary"] is None


def test_the_app_ids_are_plain_text():
    assert held_schema._key(" cm0abc123 ") == "cm0abc123"
    assert held_schema._key("") is None and held_schema._key(None) is None and held_schema._key("x" * 65) is None


def test_users_are_looked_up_never_added(monkeypatch):
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(held_schema, "get_auth_user", lookup)
    assert asyncio.run(held_schema.create_user_from_profile({"id": "new-user", "email": "a@b.test"})) is None
    lookup.assert_awaited_once_with("new-user")


def test_seeding_the_disease_list_is_refused():
    with pytest.raises(RuntimeError):
        asyncio.run(held_schema.upsert_diseases([{"id": "d", "name": "x"}]))


HELD_NOTE = {"id": "n1", "date": "2026-09-16T00:00:00.000Z", "content": "Thinking of Dad today.", "moodId": "m1",
             "mood": {"id": "m1", "code": "sad", "title": "Sad"}, "createdAt": "2026-09-16T08:00:00.000Z"}
TABLE_NOTE = {"id": "n1", "date": "2026-09-16", "content": "Thinking of Dad today.", "moodId": "m1",
              "createdAt": "2026-09-16T08:00:00+00:00", "updatedAt": "2026-09-16T08:00:00+00:00"}
MOODS = [{"id": "m1", "code": "sad", "title": "Sad", "imageUrl": None}]


@pytest.fixture
def no_notes_copy(monkeypatch, held_db):
    """On the Held database the agent's own notes table does not exist: any use of it fails the
    test. Notes are read from the app's "GriefNote" table, moods from Held's list."""
    for name in ("store_notes", "get_note", "list_notes"):
        monkeypatch.setattr(notes_store, name, AsyncMock(side_effect=AssertionError(f"{name} used")))
    monkeypatch.setattr(notes_store, "list_moods", AsyncMock(return_value=MOODS))
    monkeypatch.setattr(service, "held", AsyncMock(side_effect=AssertionError("Held API used for reading")))


def test_a_grief_note_is_read_from_the_griefnote_table(monkeypatch, no_notes_copy):
    monkeypatch.setattr(held_schema, "get_grief_note", AsyncMock(return_value=TABLE_NOTE))
    note = asyncio.run(service.get_note("usr_1", "tok", date(2026, 9, 16)))
    assert note["date"] == "2026-09-16" and note["mood"]["title"] == "Sad" and "exercise" not in note

    from backend.core.held_client import HeldApiError

    monkeypatch.setattr(held_schema, "get_grief_note", AsyncMock(return_value=None))
    with pytest.raises(HeldApiError) as missing:
        asyncio.run(service.get_note("usr_1", "tok", date(2026, 9, 17)))
    assert missing.value.status_code == 404


def test_notes_are_listed_from_the_griefnote_table(monkeypatch, no_notes_copy):
    listed = AsyncMock(return_value=([TABLE_NOTE], 21))
    monkeypatch.setattr(held_schema, "list_grief_notes", listed)
    page = asyncio.run(service.list_notes("usr_1", "tok", page=2, limit=20, date_from=date(2026, 1, 1), date_to=None))
    assert page["source"] == "database" and [n["id"] for n in page["notes"]] == ["n1"]
    assert page["pagination"] == {"page": 2, "limit": 20, "total": 21, "pages": 2, "hasNext": False, "hasPrev": True}
    listed.assert_awaited_once_with("usr_1", date(2026, 1, 1), None, 2, 20)
    assert asyncio.run(service.sync_all("usr_1", "tok")) == {"synced": 0, "stored": 21}


def test_chat_reads_grief_notes_from_the_griefnote_table(monkeypatch, no_notes_copy):
    monkeypatch.setattr(held_schema, "list_grief_notes", AsyncMock(return_value=([TABLE_NOTE], 1)))
    notes = asyncio.run(service.notes_for_chat("usr_1", "tok", "What did I write in my journal?"))
    assert notes == [{"date": "2026-09-16", "mood": "Sad", "text": "Thinking of Dad today."}]
    assert asyncio.run(service.notes_for_chat("usr_1", "tok", "I feel low.")) == []

    monkeypatch.setattr(held_schema, "list_grief_notes", AsyncMock(side_effect=RuntimeError("Database is unavailable.")))
    assert asyncio.run(service.notes_for_chat("usr_1", "tok", "Show my grief notes")) == []


def test_new_notes_are_written_through_held(monkeypatch, no_notes_copy):
    monkeypatch.setattr(service, "held", AsyncMock(return_value=HELD_NOTE))
    created = asyncio.run(service.create_note("usr_1", "tok", note_date=date(2026, 9, 16), content="Hi",
                                              mood_id="m1"))
    assert created["date"] == "2026-09-16" and "exercise" not in created
    service.held.assert_awaited_once()
    assert service.held.await_args.args[:2] == ("POST", "/notes")


@pytest.fixture
def fresh_mood_cache(monkeypatch):
    monkeypatch.setattr(notes_store, "_held_moods_cache", {"at": 0.0, "moods": None})


def test_moods_come_from_helds_public_list_and_are_cached(monkeypatch, held_db, fresh_mood_cache):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "m2", "code": "calm", "title": "Calm"}, {"id": "m1", "code": "sad", "title": "Sad"}]}

    class Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url):
            calls.append(url)
            return Response()

    monkeypatch.setattr(notes_store.httpx, "AsyncClient", Client)
    assert [m["code"] for m in asyncio.run(notes_store.list_moods())] == ["calm", "sad"]
    asyncio.run(notes_store.list_moods())
    assert len(calls) == 1 and calls[0].endswith("/moods")


def test_deleting_a_chat_where_it_is_not_allowed_answers_403(monkeypatch, held_db, fake_repo, auth):
    from fastapi.testclient import TestClient

    from backend.main import app

    monkeypatch.setattr(repository, "delete_chat_session",
                        AsyncMock(side_effect=PermissionError("Chats cannot be deleted on this database.")))
    with TestClient(app, headers=auth("usr_1")) as client:
        res = client.delete("/agentic/sessions/chat-1")
    assert res.status_code == 403 and "cannot be deleted" in res.json()["detail"]
