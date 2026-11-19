"""How rows of the Held database become the app's dicts, and what the database does not allow."""
from datetime import datetime
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from backend.db import repository
from backend.main import app


def test_a_session_uses_the_summary_columns_and_hides_the_user_memory():
    row = {
        "id": "c1", "userId": "usr_1", "title": "Work stress", "status": "ACTIVE",
        "metadata": '{"source": "app", "memory": {"user_memory": "They have a dog."}}',
        "createdAt": datetime(2026, 11, 6, 9, 0), "updatedAt": datetime(2026, 11, 6, 9, 5),
        "sessionSummary": "Facts: ...", "lastSummarizedMessageId": "m9", "totalMessageCount": 12,
    }
    session = repository._session_dict(row)
    assert session["metadata"] == {"source": "app"}  # the user memory is never shown to clients
    assert session["session_summary"] == "Facts: ..." and session["last_summarized_message_id"] == "m9"
    assert session["created_at"] == "2026-11-06T09:00:00+00:00"  # stored in UTC
    assert session["total_message_count"] == 12 and session["user_id"] == "usr_1"
    empty = repository._session_dict({**row, "metadata": None, "sessionSummary": None, "lastSummarizedMessageId": None})
    assert empty["metadata"] == {} and empty["session_summary"] is None


def test_the_app_ids_are_plain_text():
    assert repository._key(" cm0abc123 ") == "cm0abc123"
    assert repository._key("") is None and repository._key(None) is None and repository._key("x" * 65) is None


def test_a_failed_new_chat_is_reported_even_where_it_cannot_be_deleted(monkeypatch, fake_repo, auth):
    """A new chat whose first reply fails is deleted again; if the database refuses the delete,
    the user still gets the error, not a crash."""
    from backend.api.routers import agentic

    monkeypatch.setattr(repository, "delete_chat_session",
                        AsyncMock(side_effect=PermissionError("Chats cannot be deleted on this database.")))
    monkeypatch.setattr(agentic.pipeline, "run", AsyncMock(side_effect=RuntimeError("AI down")))
    with TestClient(app, headers=auth("usr_1")) as client:
        res = client.post("/agentic/run", json={"prompt": "Hello"})
    assert res.status_code == 503
