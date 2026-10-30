import asyncio
import json
from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.ai_wrapper.contracts import AuditDecision
from backend.ai_wrapper.orchestrator import AgenticPipeline
from backend.api.routers import agentic
from backend.core.config import Settings
from backend.core.held_client import HeldApiError
from backend.db import grief_notes as store
from backend.grief import service
from backend.grief.exercises import EXERCISES
from backend.main import app
from tests.test_agentic import FakeProvider

SAD = {"id": "mood-sad", "code": "sad", "title": "Sad", "imageUrl": "https://example.test/sad.png"}
GRATEFUL = {"id": "mood-grateful", "code": "grateful", "title": "Grateful", "imageUrl": None}


def held_note(day: str, content: str, mood=SAD, note_id=None):
    return {"id": note_id or f"note-{day}", "date": day, "content": content, "moodId": mood["id"], "mood": mood,
            "createdAt": f"{day}T09:00:00.000Z", "updatedAt": f"{day}T09:00:00.000Z"}


class FakeHeldNotes:
    """Held's /notes API for one user: one note per date, paging, its error codes."""

    def __init__(self, notes=()):
        self.notes = {n["date"]: n for n in notes}
        self.calls: list[tuple[str, str, str, dict]] = []
        self.down = False

    async def __call__(self, method, path, token, **kwargs):
        self.calls.append((method, path, token, kwargs))
        if self.down:
            raise HeldApiError(503, "The Held API could not be reached.", "HELD_UNAVAILABLE")
        moods = {m["id"]: m for m in (SAD, GRATEFUL)}
        if path == "/notes" and method == "GET":
            params = kwargs.get("params", {})
            ordered = sorted(self.notes.values(), key=lambda n: n["date"], reverse=True)
            ordered = [n for n in ordered if params.get("from", "0000") <= n["date"] <= params.get("to", "9999")]
            page, limit = params.get("page", 1), params.get("limit", 20)
            chunk = ordered[(page - 1) * limit: page * limit]
            pages = (len(ordered) + limit - 1) // limit
            return {"notes": chunk, "pagination": {"page": page, "limit": limit, "total": len(ordered), "pages": pages,
                                                   "hasNext": page < pages, "hasPrev": page > 1}}
        if path == "/notes" and method == "POST":
            body = kwargs["json"]
            if body["date"] in self.notes:
                raise HeldApiError(409, "A note for this date already exists", "NOTE_ALREADY_EXISTS")
            if body["moodId"] not in moods:
                raise HeldApiError(400, "Mood not found", "MOOD_NOT_FOUND")
            self.notes[body["date"]] = held_note(body["date"], body["content"], moods[body["moodId"]])
            return self.notes[body["date"]]
        day = path.rsplit("/", 1)[1]
        if day not in self.notes:
            raise HeldApiError(404, "No note for this date", "NOTE_NOT_FOUND")
        if method == "PATCH":
            body = kwargs["json"]
            note = self.notes[day]
            note.update(content=body.get("content", note["content"]))
            if "moodId" in body:
                note.update(moodId=body["moodId"], mood=moods[body["moodId"]])
        return self.notes[day]


class FakeNotesStore:
    """In-memory stand-in for backend.db.grief_notes."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.moods = [GRATEFUL, SAD]

    async def store_notes(self, user_id, notes, exercise_key=None):
        for note in notes:
            key = (user_id, note["date"][:10])
            previous = self.rows.get(key, {})
            self.rows[key] = {
                "id": note["id"], "date": note["date"][:10], "content": note["content"], "moodId": note.get("moodId"),
                "mood": note.get("mood"), "exerciseKey": exercise_key or previous.get("exerciseKey"),
                "createdAt": note.get("createdAt"), "updatedAt": note.get("updatedAt"), "syncedAt": "now",
            }

    async def set_exercise(self, user_id, note_date, exercise_key):
        row = self.rows.get((user_id, note_date.isoformat()))
        if row:
            row["exerciseKey"] = exercise_key
        return dict(row) if row else None

    async def get_note(self, user_id, note_date):
        row = self.rows.get((user_id, note_date.isoformat()))
        return dict(row) if row else None

    async def list_notes(self, user_id, date_from=None, date_to=None, page=1, limit=20):
        rows = sorted((r for (u, _), r in self.rows.items() if u == user_id
                       and (not date_from or r["date"] >= date_from.isoformat())
                       and (not date_to or r["date"] <= date_to.isoformat())),
                      key=lambda r: r["date"], reverse=True)
        return [dict(r) for r in rows[(page - 1) * limit: page * limit]], len(rows)

    async def list_moods(self):
        return list(self.moods)


@pytest.fixture
def notes_store(monkeypatch):
    fake = FakeNotesStore()
    for name in ("store_notes", "set_exercise", "get_note", "list_notes", "list_moods"):
        monkeypatch.setattr(store, name, getattr(fake, name))
    return fake


@pytest.fixture
def held(monkeypatch):
    fake = FakeHeldNotes([held_note("2026-09-16", "Thinking of Dad today."), held_note("2026-10-06", "A quiet day.")])
    monkeypatch.setattr(service, "held", fake)
    return fake


@pytest.fixture
def client(fake_repo, auth, notes_store, held):
    with TestClient(app, headers=auth("usr_1")) as test_client:
        yield test_client


def token_of(client):
    return client.headers["Authorization"].removeprefix("Bearer ")


def test_exercises_are_listed(client):
    exercises = client.get("/agentic/grief-notes/exercises").json()
    assert exercises == EXERCISES
    assert {"key", "title", "prompt"} <= set(exercises[0])


def test_moods_come_from_the_seeded_catalogue(client):
    assert [m["code"] for m in client.get("/agentic/grief-notes/moods").json()] == ["grateful", "sad"]


def test_sync_stores_every_existing_held_note(client, held, notes_store, monkeypatch):
    monkeypatch.setattr(service, "HELD_PAGE_LIMIT", 1)  # force several pages
    res = client.post("/agentic/grief-notes/sync")
    assert res.json() == {"synced": 2, "stored": 2}
    assert sorted(day for _, day in notes_store.rows) == ["2026-09-16", "2026-10-06"]
    assert {call[2] for call in held.calls} == {token_of(client)}  # the caller's own Held token


def test_list_syncs_from_held_and_stores_the_page(client, held, notes_store):
    res = client.get("/agentic/grief-notes?limit=1").json()
    assert res["source"] == "held"
    assert [n["date"] for n in res["notes"]] == ["2026-10-06"]
    assert res["notes"][0]["mood"]["title"] == "Sad" and res["notes"][0]["exercise"] is None
    assert res["pagination"]["total"] == 2
    assert ("usr_1", "2026-10-06") in notes_store.rows


def test_list_falls_back_to_the_stored_copy_when_held_is_down(client, held):
    client.post("/agentic/grief-notes/sync")
    held.down = True
    res = client.get("/agentic/grief-notes?from=2026-10-01").json()
    assert res["source"] == "stored copy (Held unreachable)"
    assert [n["date"] for n in res["notes"]] == ["2026-10-06"]
    assert res["pagination"]["total"] == 1


def test_create_resolves_the_mood_code_and_stores_the_exercise(client, held, notes_store):
    res = client.post("/agentic/grief-notes", json={
        "date": "2026-10-28", "content": "Dear Mum, I miss our walks.", "mood": "sad", "exercise": "letter_to_loved_one",
    })
    assert res.status_code == 201
    note = res.json()
    assert (note["date"], note["moodId"], note["exercise"]) == (
        "2026-10-28", "mood-sad", {"key": "letter_to_loved_one", "title": "A letter to my loved one"},
    )
    assert held.calls[-1][3]["json"] == {"date": "2026-10-28", "content": "Dear Mum, I miss our walks.", "moodId": "mood-sad"}
    assert notes_store.rows[("usr_1", "2026-10-28")]["exerciseKey"] == "letter_to_loved_one"


@pytest.mark.parametrize("body, fragment", [
    ({"date": "2026-10-28", "content": "x", "mood": "sad", "exercise": "made_up"}, "Unknown exercise"),
    ({"date": "2026-10-28", "content": "x", "mood": "ecstatic"}, "Unknown mood"),
    ({"date": "2026-10-28", "content": "x"}, "A mood is required"),
    ({"date": "2026-10-28", "content": "x" * 5001, "mood": "sad"}, "5000"),
    ({"date": "28/10/2026", "content": "x", "mood": "sad"}, "date"),
])
def test_invalid_notes_are_rejected_before_reaching_held(client, held, body, fragment):
    res = client.post("/agentic/grief-notes", json=body)
    assert res.status_code == 422
    assert fragment in json.dumps(res.json())
    assert not any(call[0] == "POST" for call in held.calls)


def test_held_errors_keep_their_status_and_code(client):
    res = client.post("/agentic/grief-notes", json={"date": "2026-09-16", "content": "Again", "mood": "sad"})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "NOTE_ALREADY_EXISTS"
    assert client.get("/agentic/grief-notes/2026-01-01").json()["detail"]["code"] == "NOTE_NOT_FOUND"


def test_editing_content_and_mood_goes_to_held(client, held, notes_store):
    res = client.patch("/agentic/grief-notes/2026-09-16", json={"content": "Thinking of Dad, smiling.", "mood": "grateful"})
    assert res.status_code == 200
    assert res.json()["content"] == "Thinking of Dad, smiling." and res.json()["mood"]["code"] == "grateful"
    assert held.calls[-1][:2] == ("PATCH", "/notes/2026-09-16")
    assert held.calls[-1][3]["json"] == {"content": "Thinking of Dad, smiling.", "moodId": "mood-grateful"}


def test_the_exercise_is_set_and_cleared_without_changing_the_held_note(client, held, notes_store):
    note = client.patch("/agentic/grief-notes/2026-09-16", json={"exercise": "memory_to_keep"}).json()
    assert note["exercise"]["key"] == "memory_to_keep" and note["content"] == "Thinking of Dad today."
    assert not any(call[0] == "PATCH" for call in held.calls)
    assert client.patch("/agentic/grief-notes/2026-09-16", json={"exercise": None}).json()["exercise"] is None
    assert client.patch("/agentic/grief-notes/2026-09-16", json={}).status_code == 422


def test_notes_cannot_be_deleted_and_need_sign_in(client, fake_repo, notes_store, held):
    assert client.delete("/agentic/grief-notes/2026-09-16").status_code == 405
    with TestClient(app) as anonymous:
        assert anonymous.get("/agentic/grief-notes").status_code == 401


# -- chat ----------------------------------------------------------------------

def test_chat_gets_recent_notes_only_when_the_message_refers_to_them(notes_store, held):
    assert asyncio.run(service.notes_for_chat("usr_1", "tok", "I feel low today")) == []
    assert held.calls == []
    notes = asyncio.run(service.notes_for_chat("usr_1", "tok", "What did I write in my journal last month?"))
    assert notes == [
        {"date": "2026-10-06", "mood": "Sad", "text": "A quiet day."},
        {"date": "2026-09-16", "mood": "Sad", "text": "Thinking of Dad today."},
    ]


def test_chat_uses_the_stored_copy_when_held_is_down(notes_store, held):
    asyncio.run(service.notes_for_chat("usr_1", "tok", "my notes"))
    held.down = True
    asyncio.run(notes_store.set_exercise("usr_1", date(2026, 9, 16), "memory_to_keep"))
    notes = asyncio.run(service.notes_for_chat("usr_1", "tok", "show my grief notes"))
    assert notes[1] == {"date": "2026-09-16", "mood": "Sad", "exercise": "A memory I want to keep",
                        "text": "Thinking of Dad today."}


def test_notes_reach_the_writer_and_the_auditor(fake_repo, auth, notes_store, held, monkeypatch):
    provider = FakeProvider(route="GRIEF")
    monkeypatch.setattr(agentic, "pipeline", AgenticPipeline(provider, Settings(_env_file=None)))
    monkeypatch.setattr(agentic, "notes_for_chat", service.notes_for_chat)  # undo the conftest stub
    monkeypatch.setattr(agentic, "generate_title", AsyncMock(return_value="Title"))
    with TestClient(app, headers=auth("usr_1")) as client:
        assert client.post("/agentic/run", json={"prompt": "What did I write in my journal about Dad?"}).status_code == 200
        client.post("/agentic/run", json={"prompt": "I feel low."})
    assert [n["date"] for n in provider.payloads[0]["grief_notes"]] == ["2026-10-06", "2026-09-16"]
    audit_inputs = [json.loads(text) for model, text in provider.structured_inputs if model is AuditDecision]
    assert audit_inputs[0]["grief_notes"] == provider.payloads[0]["grief_notes"]
    assert "grief_notes" not in provider.payloads[-1]  # a message that does not mention notes
