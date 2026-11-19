import json

import pytest
from fastapi.testclient import TestClient

from backend.api.routers import agentic
from backend.main import app


@pytest.fixture
def client(monkeypatch, fake_repo, auth):
    async def run(*, prompt, memory=None, profile=None, recall=None, note_tools=None, is_note=False):
        return f"Answer to: {prompt}"

    async def title(*args):
        return "Generated title"

    monkeypatch.setattr(agentic.pipeline, "run", run)
    monkeypatch.setattr(agentic, "generate_title", title)
    monkeypatch.setattr(agentic.settings, "agentic_stream_delay_seconds", 0.0)
    with TestClient(app, headers=auth("usr_1")) as test_client:
        yield test_client


def _events(res):
    return [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data:")]


def test_health_endpoint(client):
    res = client.get("/agentic/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert res.json()["database"]["connected"] is True


def test_another_users_chat_cannot_be_continued(client, auth, fake_repo):
    sid = client.post("/agentic/run", json={"prompt": "Mine"}).json()["session_id"]
    res = client.post("/agentic/run", json={"prompt": "Let me in", "session_id": sid}, headers=auth("usr_2"))
    assert res.status_code == 404
    assert [m["content"] for m in fake_repo.messages[sid]] == ["Mine", "Answer to: Mine"]  # untouched


def test_the_reply_returns_the_chat_with_its_title_and_count(client):
    data = client.post("/agentic/run", json={"prompt": "Hello"}).json()
    assert data["session"]["title"] == "Generated title" and data["session"]["total_message_count"] == 2


def test_run_stores_client_metadata_but_ignores_body_user_id(client, fake_repo):
    payload = {"prompt": "I need grief advice", "user_id": "usr_2", "metadata": {"source": "mobile_app"}}
    data = client.post("/agentic/run", json=payload).json()
    session = fake_repo.sessions[data["session_id"]]
    assert session["user_id"] == "usr_1"
    assert session["metadata"] == {"source": "mobile_app"}
    assert data["session"]["title"] == "Generated title"


def test_run_appends_to_existing_session(client, fake_repo):
    sid = client.post("/agentic/run", json={"prompt": "First"}).json()["session_id"]
    data = client.post("/agentic/run", json={"prompt": "Another question", "session_id": sid}).json()
    assert data["session_id"] == sid
    assert len(fake_repo.sessions) == 1
    assert fake_repo.sessions[sid]["total_message_count"] == 4


def test_stream_creates_session_then_appends(client, fake_repo):
    first = _events(client.post("/agentic/stream", json={"prompt": "Stream prompt"}))
    sid = first[0]["sessionId"]
    assert first[-1]["done"] is True and first[-1]["sessionId"] == sid
    assert "".join(e.get("text", "") for e in first) == "Answer to: Stream prompt"

    second = _events(client.post("/agentic/stream", json={"prompt": "Followup", "session_id": sid}))
    assert second[0]["sessionId"] == sid and second[-1]["done"] is True
    assert [m["content"] for m in fake_repo.messages[sid]] == [
        "Stream prompt", "Answer to: Stream prompt", "Followup", "Answer to: Followup",
    ]
