import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.db import repository
from backend.main import app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from seed_diseases import fetch_catalogue  # noqa: E402

CATALOGUE = [
    {"id": "4f435b39-a551-493b-8fd2-267cd9bbffd3", "name": "Anxiety Disorder", "abbreviation": None,
     "imageUrl": "https://example.test/anxiety_disorder.png"},
    {"id": "8b1798a6-1777-474f-8f50-d6fbe43abcac", "name": "Attention-Deficit/Hyperactivity Disorder",
     "abbreviation": "ADHD", "imageUrl": None},
]


def held(payload, status_code=200):
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status_code, json=payload)))


def test_catalogue_is_read_from_held_diseases():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"success": True, "data": [*CATALOGUE, {"id": "x", "name": "  "}, "junk"]})

    diseases = fetch_catalogue("http://held.test/api/", httpx.Client(transport=httpx.MockTransport(handler)))
    assert requests == ["http://held.test/api/diseases"]
    assert diseases == CATALOGUE  # blank and malformed entries are skipped


@pytest.mark.parametrize("payload", [{"data": []}, {"data": "nope"}, {}])
def test_an_empty_or_malformed_catalogue_changes_nothing(payload):
    with pytest.raises(ValueError):
        fetch_catalogue("http://held.test/api", held(payload))


def test_held_errors_stop_the_seed():
    with pytest.raises(httpx.HTTPStatusError):
        fetch_catalogue("http://held.test/api", held({"message": "down"}, status_code=503))


def test_diseases_endpoint_lists_the_seeded_catalogue(fake_repo, auth, monkeypatch):
    monkeypatch.setattr(repository, "list_diseases", AsyncMock(return_value=CATALOGUE))
    with TestClient(app, headers=auth("usr_1")) as client:
        res = client.get("/agentic/diseases")
    assert res.status_code == 200
    assert res.json() == CATALOGUE


def test_diseases_endpoint_reports_an_unavailable_database(fake_repo, auth, monkeypatch):
    monkeypatch.setattr(repository, "list_diseases", AsyncMock(side_effect=RuntimeError("Database is unavailable.")))
    with TestClient(app, headers=auth("usr_1")) as client:
        assert client.get("/agentic/diseases").status_code == 503


def test_diseases_endpoint_requires_sign_in(fake_repo):
    with TestClient(app) as client:
        assert client.get("/agentic/diseases").status_code == 401
