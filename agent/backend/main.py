from contextlib import asynccontextmanager

from fastapi import FastAPI

# RAG disabled: documents, the router behind every upload and search endpoint.
# from backend.api.routers import agentic, documents, grief_notes, sessions
from backend.api.routers import agentic, grief_notes, sessions
from backend.core.auth import logger as auth_logger
from backend.core.config import get_settings
from backend.db.connection import close_db_pool


@asynccontextmanager
async def lifespan(_: FastAPI):
    if get_settings().auth_disabled:
        auth_logger.warning("AUTH_DISABLED is on: requests need no token and share one guest user.")
    # RAG disabled: documents whose embedding was interrupted continue in the background.
    # await documents.ingestion_worker.resume_unfinished()
    try:
        yield
    finally:
        # await documents.ingestion_worker.stop()  # RAG disabled
        await agentic.gemini_client.close()
        await close_db_pool()


app = FastAPI(
    title=get_settings().app_name,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(agentic.router)
app.include_router(sessions.router)
# app.include_router(documents.router)  # RAG disabled
app.include_router(grief_notes.router)


@app.get("/", tags=["Health"], summary="Public liveness check")
async def liveness():
    # Touches no database or Held API, so it answers whenever the process is up.
    return {"status": "ok", "app_name": get_settings().app_name}
