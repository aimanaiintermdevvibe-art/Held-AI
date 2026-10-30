from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api.routers import agentic, documents, grief_notes, sessions
from backend.core.config import get_settings
from backend.db.connection import close_db_pool


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Documents whose embedding was interrupted (e.g. by a restart) continue in the background.
    await documents.ingestion_worker.resume_unfinished()
    try:
        yield
    finally:
        await documents.ingestion_worker.stop()
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
app.include_router(documents.router)
app.include_router(grief_notes.router)


@app.get("/", tags=["Health"], summary="Public liveness check")
async def liveness():
    # Touches no database or Held API, so it answers whenever the process is up.
    return {"status": "ok", "app_name": get_settings().app_name}
