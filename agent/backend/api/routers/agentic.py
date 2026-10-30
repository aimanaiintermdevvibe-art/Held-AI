import asyncio
import json
import logging
import re

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi import Depends

from backend.ai_wrapper.client import GeminiClient, ProviderBusyError
from backend.ai_wrapper.memory import build_context, generate_title, refresh_summary
from backend.ai_wrapper.orchestrator import AgenticPipeline
from backend.core.config import get_settings
from backend.core.auth import AccessTokenPayload, fetch_user_profile, require_auth
from backend.schemas.agentic import AgenticRunRequest, AgenticRunResponse
from backend.db import repository
from backend.grief.service import notes_for_chat
from backend.rag.service import as_excerpts, as_sources, excerpts_for_chat

logger = logging.getLogger("held_ai.agentic")

# Generation requires a signed-in user that exists in public."User": every turn
# is stored in chat_sessions / chat_messages and remembered on later turns.
router = APIRouter(prefix="/agentic")
settings = get_settings()
gemini_client = GeminiClient(settings)
pipeline = AgenticPipeline(gemini_client, settings)

# ---------------------------------------------------------------------------
# Background summarisation queue
#
# Summary work is enqueued here after a turn completes so the user never waits
# for the LLM summarisation call.  A single long-running consumer task drains
# the queue one job at a time, ensuring the database is updated when idle.
# ---------------------------------------------------------------------------
_summary_queue: asyncio.Queue | None = None
_consumer_task: asyncio.Task | None = None


async def _summary_consumer(queue: asyncio.Queue) -> None:
    """Long-running task that drains the summarisation queue.

    Each item is a coroutine factory (zero-arg async callable).  Running them
    serially keeps DB write pressure low and avoids concurrent summary races on
    the same session.
    """
    while True:
        job = await queue.get()
        try:
            await job()
        except Exception as exc:
            logger.warning("Background summary job failed: %s", exc)
        finally:
            queue.task_done()


def _ensure_consumer() -> asyncio.Queue:
    """Start (or restart, on a new event loop) the consumer and return its queue."""
    global _summary_queue, _consumer_task
    loop = asyncio.get_running_loop()
    queue = _summary_queue
    if (queue is None or _consumer_task is None or _consumer_task.done()
            or _consumer_task.get_loop() is not loop):
        queue = _summary_queue = asyncio.Queue()
        _consumer_task = loop.create_task(_summary_consumer(queue), name="summary_consumer")
    return queue


def _enqueue_summary(session_id: str) -> None:
    """Schedule a background summary check; the job re-reads the session from the DB."""
    queue = _ensure_consumer()

    async def _do_summary() -> None:
        session = await repository.get_chat_session(session_id)
        if not session:
            return
        messages = await repository.get_session_messages(session_id)
        state = await refresh_summary(gemini_client, settings, session, messages)
        if state:
            await repository.update_chat_session(session_id, **state)
            logger.debug("Background summary done for session %s", session_id)

    queue.put_nowait(_do_summary)


@router.get(
    "/health",
    tags=["Health"],
    summary="Agent API & DB Health",
    dependencies=[Depends(require_auth)],
)
async def health_check():
    db_overview = await repository.get_database_overview()
    return {
        "status": "ok",
        "app_name": settings.app_name,
        "database": {
            "connected": db_overview.get("connected", False),
            "counts": db_overview.get("counts", {}),
        },
    }


def _extract_session_params(payload: AgenticRunRequest) -> tuple[str | None, str | None, dict | None]:
    nested = payload.session or payload.session_data or {}
    session_id = payload.session_id or nested.get("id") or nested.get("session_id")
    title = payload.title or nested.get("title")
    metadata = payload.metadata or nested.get("metadata")
    return (
        str(session_id).strip() if session_id else None,
        str(title).strip() if title else None,
        metadata if isinstance(metadata, dict) else None,
    )


async def _open_conversation(
    payload: AgenticRunRequest, principal: AccessTokenPayload,
) -> tuple[dict, list[dict], dict | None, bool]:
    """Load (or create) the caller's session, its history, and the memory context."""
    req_session_id, _, meta_param = _extract_session_params(payload)
    owner_id = None if principal["role"] == "ADMIN" else principal["user_id"]
    history: list[dict] = []
    if req_session_id:
        session = await repository.get_chat_session(req_session_id, user_id=owner_id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found.")
        history = await repository.get_session_messages(session["id"])
        is_new = False
    else:
        excerpt = payload.prompt[:40].strip() + ("..." if len(payload.prompt) > 40 else "")
        session = await repository.create_chat_session(
            user_id=principal["user_id"], title=excerpt or "New Conversation",
            metadata=_client_metadata(meta_param),
        )
        if not session:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not start a conversation. Please try again.",
            )
        is_new = True
    memory = await _load_memory(session, history)
    return session, history, memory, is_new


@router.post(
    "/run", response_model=AgenticRunResponse, response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def run_agentic(payload: AgenticRunRequest, request: Request) -> AgenticRunResponse:
    principal: AccessTokenPayload = request.state.user
    session, _, memory, is_new = await _open_conversation(payload, principal)
    try:
        profile = await _profile(request, principal)
        async with asyncio.timeout(settings.agentic_run_timeout_seconds):
            retrieved = await excerpts_for_chat(
                gemini_client, settings, user_id=principal["user_id"], prompt=payload.prompt,
            )
            notes = await notes_for_chat(principal["user_id"], request.state.access_token, payload.prompt)
            response = await pipeline.run(
                prompt=payload.prompt, memory=memory, profile=profile, documents=as_excerpts(retrieved),
                notes=notes,
            )
    except Exception as exc:
        await _discard_if_new(session, is_new)
        status_code, detail, headers = _failure(exc)
        raise HTTPException(status_code=status_code, detail=detail, headers=headers) from exc

    await _finish_turn(session, is_new, payload.prompt, response)
    return AgenticRunResponse(
        response=response, session_id=session["id"], session=session,
        sources=as_sources(retrieved) or None,
    )


@router.post("/stream", dependencies=[Depends(require_auth)])
async def stream_agentic(payload: AgenticRunRequest, request: Request) -> StreamingResponse:
    principal: AccessTokenPayload = request.state.user
    session, _, memory, is_new = await _open_conversation(payload, principal)

    async def events():
        # 1. The client gets the session ID before any answer text.
        yield _sse({"sessionId": session["id"], "session_id": session["id"], "session": session})

        try:
            profile = await _profile(request, principal)
            async with asyncio.timeout(settings.agentic_run_timeout_seconds):
                retrieved = await excerpts_for_chat(
                    gemini_client, settings, user_id=principal["user_id"], prompt=payload.prompt,
                )
                notes = await notes_for_chat(principal["user_id"], request.state.access_token, payload.prompt)
                # Keep the complete answer behind the existing safety audit before
                # sending any user-visible text to the client.
                response = await pipeline.run(
                    prompt=payload.prompt, memory=memory, profile=profile, documents=as_excerpts(retrieved),
                    notes=notes,
                )
        except Exception as exc:
            await _discard_if_new(session, is_new)
            status_code, detail, headers = _failure(exc)
            event: dict[str, object] = {"detail": detail, "status": status_code}
            if headers:
                event["retry_after"] = int(headers["Retry-After"])
            if is_new:
                event["session_discarded"] = True
            yield _sse(event)
            return

        chunks = _chunk_text(response, settings.agentic_stream_chunk_size)
        for chunk in chunks:
            yield _sse({"text": chunk})
            if settings.agentic_stream_delay_seconds > 0:
                await asyncio.sleep(settings.agentic_stream_delay_seconds)

        # 2. Persist the turn and enqueue a background summary (non-blocking).
        await _finish_turn(session, is_new, payload.prompt, response)
        done: dict[str, object] = {"done": True, "sessionId": session["id"], "session_id": session["id"], "session": session}
        if retrieved:
            done["sources"] = as_sources(retrieved)
        yield _sse(done)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _profile(request: Request, principal: AccessTokenPayload) -> dict:
    """The caller's Held profile; authentication may already have fetched it."""
    profile = getattr(request.state, "held_profile", None)
    if profile is not None:
        return profile
    return await fetch_user_profile(request.state.access_token, principal["user_id"])


async def _load_memory(session: dict, history: list[dict]) -> dict | None:
    """This conversation's history plus recaps of the user's other recent conversations."""
    previous: list[dict] = []
    try:
        previous = await repository.get_previous_conversations(
            session["user_id"], exclude_session_id=session["id"],
        )
    except Exception as exc:
        logger.warning("Could not load previous conversations: %s", exc)
    return build_context(history, session, settings.memory_context_char_budget, previous)


def _failure(exc: Exception) -> tuple[int, str, dict[str, str] | None]:
    """Map a generation failure to (status, user-facing message, headers)."""
    if isinstance(exc, ProviderBusyError):
        wait = exc.retry_after or 60
        return (
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"The AI is getting too many requests right now. Please try again in about {wait} seconds.",
            {"Retry-After": str(wait)},
        )
    if isinstance(exc, TimeoutError):
        return status.HTTP_504_GATEWAY_TIMEOUT, "Agent response timed out.", None
    if isinstance(exc, (RuntimeError, ValueError)):
        return status.HTTP_503_SERVICE_UNAVAILABLE, "Agent service is unavailable.", None
    raise exc


async def _discard_if_new(session: dict, is_new: bool) -> None:
    """A brand-new chat whose first answer failed holds no messages; don't leave it behind."""
    if not is_new:
        return
    try:
        await repository.delete_chat_session(session["id"])
    except Exception as exc:
        logger.warning("Could not discard empty session %s: %s", session["id"], exc)


def _client_metadata(metadata: dict | None) -> dict | None:
    """Client metadata may not overwrite server-owned keys."""
    return {k: v for k, v in metadata.items() if k != "memory"} if metadata else metadata


async def _finish_turn(session: dict, is_new: bool, prompt: str, response: str) -> None:
    """Save both sides of the turn, name new sessions, then queue a summary check.

    Message persistence is awaited to guarantee durability before the response
    completes; summarisation runs in the background so the user never waits.
    """
    session_id = session["id"]
    try:
        await repository.save_chat_message(session_id=session_id, role="user", content=prompt)
        saved = await repository.save_chat_message(session_id=session_id, role="assistant", content=response)
        if saved:
            session["total_message_count"] = saved.get("total_message_count", session.get("total_message_count"))
        if is_new:
            title = await generate_title(gemini_client, settings, prompt, response)
            await repository.update_chat_session(session_id, title=title)
            session["title"] = title
    except Exception as exc:
        logger.error("Could not persist turn for session %s: %s", session_id, exc)
        return
    _enqueue_summary(session_id)


def _chunk_text(text: str, target_size: int = 24) -> list[str]:
    """Split text into natural word-aware chunks of roughly target_size characters."""
    if not text:
        return []
    words = re.findall(r"\S+\s*|\s+", text)
    if not words:
        return [text]

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_length = 0

    for word in words:
        if len(word) > target_size * 2:
            if current_chunk:
                chunks.append("".join(current_chunk))
                current_chunk = []
                current_length = 0
            for i in range(0, len(word), target_size):
                chunks.append(word[i:i + target_size])
            continue

        if current_length + len(word) > target_size and current_chunk:
            chunks.append("".join(current_chunk))
            current_chunk = [word]
            current_length = len(word)
        else:
            current_chunk.append(word)
            current_length += len(word)

    if current_chunk:
        chunks.append("".join(current_chunk))

    return chunks


def _sse(data: dict[str, object]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
