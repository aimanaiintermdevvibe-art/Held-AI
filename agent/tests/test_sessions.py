import json

import pytest
from fastapi.testclient import TestClient

from backend.api.routers import agentic
from backend.main import app


@pytest.fixture
def client(monkeypatch, fake_repo, auth):
    async def run(*, prompt, memory=None, profile=None, documents=None, notes=None):
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


def test_list_sessions_is_scoped_to_the_caller(client, auth):
    client.post("/agentic/run", json={"prompt": "Mine"})
    client.post("/agentic/run", json={"prompt": "Theirs"}, headers=auth("usr_2"))
    mine = client.get("/agentic/sessions").json()
    assert [s["user_id"] for s in mine] == ["usr_1"]
    assert mine[0]["title"] == "Generated title"
    assert mine[0]["total_message_count"] == 2
    assert len(client.get("/agentic/sessions", headers=auth("adm_1")).json()) == 2


def test_post_session_endpoint_disallowed(client):
    res = client.post("/agentic/sessions", json={"title": "Caregiver Support", "user_id": "usr_1"})
    assert res.status_code == 405


def test_get_session_details_endpoint_disallowed(client):
    res = client.get("/agentic/sessions/22006c3e-dc6f-4862-9503-5d408ddc6c97")
    assert res.status_code == 405


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


def test_get_session_messages_endpoint(client, auth):
    sid = client.post("/agentic/run", json={"prompt": "Hello"}).json()["session_id"]
    data = client.get(f"/agentic/sessions/{sid}/messages").json()
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
    assert data["session"]["total_message_count"] == 2
    assert client.get(f"/agentic/sessions/{sid}/messages", headers=auth("usr_2")).status_code == 404


def test_delete_session_endpoint(client, auth, fake_repo):
    sid = client.post("/agentic/run", json={"prompt": "Hello"}).json()["session_id"]
    assert client.delete(f"/agentic/sessions/{sid}", headers=auth("usr_2")).status_code == 404
    res = client.delete(f"/agentic/sessions/{sid}")
    assert res.status_code == 200 and res.json() == {"success": True}
    assert sid not in fake_repo.sessions


def test_users_endpoints_are_admin_only_and_read_only(client, auth):
    assert client.get("/agentic/users").status_code == 403
    admin = auth("adm_1")
    assert len(client.get("/agentic/users", headers=admin).json()) == 3
    assert client.get("/agentic/users/usr_1", headers=admin).json()["role"] == "USER"
    assert client.post("/agentic/users", json={"email": "b@example.com"}, headers=admin).status_code == 405


def test_diseases_are_read_only_and_need_the_database(client):
    assert client.get("/agentic/diseases").status_code == 503  # tests run without a database
    assert client.post("/agentic/diseases", json={"name": "New"}).status_code == 405
