from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api.routers import agentic, sessions
from backend.core.config import get_settings
from backend.db.connection import close_db_pool


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        yield
    finally:
        await agentic.gemini_client.close()
        await close_db_pool()


def docs_urls(show: bool) -> dict:
    """Swagger at /docs only when SHOW_API_DOCS is on (local testing); otherwise no docs at all."""
    return {"docs_url": "/docs" if show else None, "redoc_url": None,
            "openapi_url": "/openapi.json" if show else None}


app = FastAPI(title=get_settings().app_name, lifespan=lifespan, **docs_urls(get_settings().show_api_docs))
app.include_router(agentic.router)
app.include_router(sessions.router)


def _openapi_with_bearer() -> dict:
    """The schema with a Bearer token field, so Swagger's Authorize button can send a Held token."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = {
        "HeldToken": {"type": "http", "scheme": "bearer", "description": "A Held access token"},
    }
    schema["security"] = [{"HeldToken": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_with_bearer


@app.get("/", tags=["Health"], summary="Public liveness check")
async def liveness():
    # Touches no database or Held API, so it answers whenever the process is up.
    return {"status": "ok", "app_name": get_settings().app_name}
