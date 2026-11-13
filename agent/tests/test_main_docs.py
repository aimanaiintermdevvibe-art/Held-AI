"""Swagger is served only when SHOW_API_DOCS is on (local testing), never by default."""

from backend.core.config import Settings
from backend.main import app, docs_urls


def test_docs_are_off_by_default():
    assert Settings(_env_file=None).show_api_docs is False
    assert docs_urls(False) == {"docs_url": None, "redoc_url": None, "openapi_url": None}


def test_docs_can_be_switched_on_for_local_testing():
    assert docs_urls(True) == {"docs_url": "/docs", "redoc_url": None, "openapi_url": "/openapi.json"}


def test_the_schema_offers_a_bearer_token_field():
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["HeldToken"] == {
        "type": "http", "scheme": "bearer", "description": "A Held access token"}
