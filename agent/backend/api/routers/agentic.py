import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi import Depends

from backend.ai_wrapper.client import GeminiClient, ProviderBusyError
from backend.ai_wrapper.memory import (
    RECENT_MESSAGES,
    build_context,
    build_user_memory,
    generate_title,
    refresh_summary,
    summary_due,
    unsummarized,
)
from backend.ai_wrapper.orchestrator import AgenticPipeline
from backend.ai_wrapper.usage import UsageTracker, log_usage, track_usage
from backend.core.config import get_settings
from backend.core.auth import AccessTokenPayload, fetch_user_profile, require_auth
from backend.schemas.agentic import AgenticRunRequest, AgenticRunResponse
from backend.db import repository
from backend.grief.service import notes_for_chat
# RAG disabled: DSM-5-TR knowledge now comes from the agent prompts.
# from backend.rag.service import as_excerpts, as_sources, excerpts_for_chat

logger = logging.getLogger("held_ai.agentic")

# Generation requires a signed-in user that exists in public."User": every turn
# is stored in chat_sessions / chat_messages and remembered on later turns.
router = APIRouter(prefix="/agentic")
settings = get_settings()
gemini_client = GeminiClient(settings)
pipeline = AgenticPipeline(gemini_client, settings)
HEALTH_DB_TIMEOUT_SECONDS = 5.0

# ---------------------------------------------------------------------------
# Background queue
#
# After a turn is saved, the rolling summary (when one is due) and, for a new
# chat, the user memory are updated here, so the user never waits for those
# model calls. A single long-running consumer task drains the queue one job at
# a time, which also keeps two summaries of one chat from racing.
# ---------------------------------------------------------------------------
_summary_queue: asyncio.Queue | None = None
_consumer_task: asyncio.Task | None = None


async def _summary_consumer(queue: asyncio.Queue) -> None:
    """Long-running task that drains the background queue.

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


def _enqueue(job) -> None:
    _ensure_consumer().put_nowait(job)


def _summary_job(session_id: str, known: list[dict]):
    """Fold the chat's unsummarized messages into its rolling summary. known are the messages
    the turn already had (the latest ones, with the two it saved), so the job reads only the
    session row, to see whether another job summarized meanwhile, and reads messages only when
    the summary has fallen further behind than known reaches."""

    async def job() -> None:
        tracker = UsageTracker()
        with track_usage(tracker):
            session = await repository.get_chat_session(session_id)
            if not session:
                return
            anchor = session.get("last_summarized_message_id")
            pending = unsummarized(session, known)
            if pending is None:
                pending = await repository.get_messages_after(session_id, anchor)
            state = await refresh_summary(gemini_client, settings, session, pending)
        log_usage("summary", tracker, session=session_id)
        if state:
            await repository.save_session_summary(session_id, previous_anchor=anchor, **state)

    return job


def _user_memory_job(user_id: str, previous: list[dict]):
    """Rebuild the user memory from the recaps a new chat's first reply already loaded."""

    async def job() -> None:
        tracker = UsageTracker()
        with track_usage(tracker):
            summary = await build_user_memory(gemini_client, settings, previous)
        log_usage("user_memory", tracker, user=user_id)
        if summary:
            await repository.save_user_memory(user_id, summary)

    return job


@router.get("/health", tags=["Health"], summary="Agent API & DB Health")
async def health_check():
    # Public: reports no row counts (see /agentic/admin/overview), and an unreachable
    # database must not hold the request for the pool's 60 s connect timeout.
    try:
        async with asyncio.timeout(HEALTH_DB_TIMEOUT_SECONDS):
            connected = await repository.ping_database()
    except Exception as exc:
        logger.warning("Health check could not reach the database: %r", exc)
        connected = False
    return {
        "status": "ok" if connected else "degraded",
        "app_name": settings.app_name,
        "database": {"connected": connected},
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


@dataclass
class _Chat:
    session: dict
    history: list[dict]  # this chat's latest stored messages (at most RECENT_MESSAGES), oldest first
    memory: dict | None
    is_new: bool
    previous: list[dict] = field(default_factory=list)  # recaps of other chats, for a new chat only


async def _open_conversation(payload: AgenticRunRequest, principal: AccessTokenPayload) -> _Chat:
    """An existing chat: the session, its latest messages and the user memory, in one read.
    A new chat: the session is created, and recaps of the user's other recent chats are read."""
    req_session_id, _, meta_param = _extract_session_params(payload)
    owner_id = None if principal["role"] == "ADMIN" else principal["user_id"]
    if req_session_id:
        opened = await repository.open_chat(req_session_id, user_id=owner_id, recent=RECENT_MESSAGES)
        if not opened:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found.")
        session, history, earlier = opened
        return _Chat(session, history, build_context(history, session, earlier=earlier), is_new=False)

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
    previous = await _previous_conversations(session)
    return _Chat(session, [], build_context([], session, previous=previous), is_new=True, previous=previous)


@router.post(
    "/run", response_model=AgenticRunResponse, response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def run_agentic(payload: AgenticRunRequest, request: Request) -> AgenticRunResponse:
    principal: AccessTokenPayload = request.state.user
    chat = await _open_conversation(payload, principal)
    session = chat.session
    tracker = UsageTracker()
    try:
        profile = await _profile(request, principal)
        async with asyncio.timeout(settings.agentic_run_timeout_seconds):
            # RAG disabled:
            # retrieved = await excerpts_for_chat(
            #     gemini_client, settings, user_id=principal["user_id"], prompt=payload.prompt,
            # )
            notes = await notes_for_chat(principal["user_id"], request.state.access_token, payload.prompt)
            with track_usage(tracker):
                response = await pipeline.run(
                    prompt=payload.prompt, memory=chat.memory, profile=profile,
                    # documents=as_excerpts(retrieved),  # RAG disabled
                    notes=notes,
                )
    except Exception as exc:
        log_usage("failed_turn", tracker, session=session["id"])
        await _discard_if_new(session, chat.is_new)
        status_code, detail, headers = _failure(exc)
        raise HTTPException(status_code=status_code, detail=detail, headers=headers) from exc

    await _finish_turn(chat, payload.prompt, response, tracker)
    return AgenticRunResponse(
        response=response, session_id=session["id"], session=session,
        # sources=as_sources(retrieved) or None,  # RAG disabled
    )


@router.post("/stream", dependencies=[Depends(require_auth)])
async def stream_agentic(payload: AgenticRunRequest, request: Request) -> StreamingResponse:
    principal: AccessTokenPayload = request.state.user
    chat = await _open_conversation(payload, principal)
    session, is_new = chat.session, chat.is_new
    tracker = UsageTracker()

    async def events():
        # 1. The client gets the session ID before any answer text.
        yield _sse({"sessionId": session["id"], "session_id": session["id"], "session": session})

        try:
            profile = await _profile(request, principal)
            async with asyncio.timeout(settings.agentic_run_timeout_seconds):
                # RAG disabled:
                # retrieved = await excerpts_for_chat(
                #     gemini_client, settings, user_id=principal["user_id"], prompt=payload.prompt,
                # )
                notes = await notes_for_chat(principal["user_id"], request.state.access_token, payload.prompt)
                # Keep the complete answer behind the existing safety audit before
                # sending any user-visible text to the client.
                with track_usage(tracker):
                    response = await pipeline.run(
                        prompt=payload.prompt, memory=chat.memory, profile=profile,
                        # documents=as_excerpts(retrieved),  # RAG disabled
                        notes=notes,
                    )
        except Exception as exc:
            log_usage("failed_turn", tracker, session=session["id"])
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

        # 2. Persist the turn and queue the background memory updates (non-blocking).
        await _finish_turn(chat, payload.prompt, response, tracker)
        done: dict[str, object] = {"done": True, "sessionId": session["id"], "session_id": session["id"], "session": session}
        # RAG disabled:
        # if retrieved:
        #     done["sources"] = as_sources(retrieved)
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


async def _previous_conversations(session: dict) -> list[dict]:
    """Recaps of the user's other recent conversations, for a new chat's first reply."""
    try:
        return await repository.get_previous_conversations(
            session["user_id"], exclude_session_id=session["id"],
        )
    except Exception as exc:
        logger.warning("Could not load previous conversations: %s", exc)
        return []


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


async def _finish_turn(chat: _Chat, prompt: str, response: str, tracker: UsageTracker) -> None:
    """Save the turn in one write (with a new chat's AI-written title), log its token use, then
    queue the memory updates that are due: the rolling summary every SUMMARY_THRESHOLD
    messages, and the user memory when a chat starts.

    Saving is awaited, so the turn is stored before the response completes; the memory
    updates run in the background so the user never waits.
    """
    session = chat.session
    title = None
    if chat.is_new:
        with track_usage(tracker):
            title = await generate_title(gemini_client, settings, prompt, response)
    log_usage("turn", tracker, session=session["id"])
    try:
        saved = await repository.save_turn(session["id"], prompt, response, title=title)
    except Exception as exc:
        logger.error("Could not persist turn for session %s: %s", session["id"], exc)
        return
    if not saved:
        return
    session.update(
        total_message_count=saved["total_message_count"], title=saved["title"], updated_at=saved["updated_at"],
    )
    known = chat.history + saved["messages"]
    if summary_due(session, known):
        _enqueue(_summary_job(session["id"], known))
    if chat.is_new and chat.previous:
        _enqueue(_user_memory_job(session["user_id"], chat.previous))


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
