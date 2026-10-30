import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from backend.core.auth import AccessTokenPayload, require_auth
from backend.core.held_client import HeldApiError
from backend.db import grief_notes as store
from backend.grief import service
from backend.grief.exercises import EXERCISES
from backend.schemas.grief_notes import GriefNoteCreate, GriefNoteUpdate

logger = logging.getLogger("held_ai.api.grief_notes")

# The Held app's grief notes (one per user per date, with a mood), written and read through
# Held's API with the caller's own token and stored per user in public.grief_notes, plus the
# guided workbook exercises a note can answer. Held has no delete for notes.
router = APIRouter(prefix="/agentic/grief-notes", tags=["Grief notes"], dependencies=[Depends(require_auth)])


def _held_error(exc: HeldApiError) -> HTTPException:
    """Held's own status and code (e.g. 409 NOTE_ALREADY_EXISTS, 403 ONBOARDING_INCOMPLETE)."""
    status_code = exc.status_code if 400 <= exc.status_code < 500 else status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": str(exc)})


def _storage_error() -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Grief notes storage is unavailable.")


async def _mood_id(mood_id: Optional[str], mood_code: Optional[str]) -> Optional[str]:
    if mood_id or not mood_code:
        return mood_id
    moods = {m["code"]: m["id"] for m in await store.list_moods()}
    if mood_code not in moods:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown mood {mood_code!r}; use one of: {', '.join(sorted(moods)) or 'none seeded yet'}.",
        )
    return moods[mood_code]


@router.get("/exercises", summary="Guided grief-workbook exercises")
async def list_exercises() -> List[Dict[str, str]]:
    return EXERCISES


@router.get("/moods", summary="Moods a note can carry")
async def list_moods() -> List[Dict[str, Any]]:
    try:
        return await store.list_moods()
    except RuntimeError as exc:
        raise _storage_error() from exc


@router.post("/sync", summary="Store all of your Held grief notes")
async def sync_notes(request: Request) -> Dict[str, int]:
    principal: AccessTokenPayload = request.state.user
    try:
        return await service.sync_all(principal["user_id"], request.state.access_token)
    except HeldApiError as exc:
        raise _held_error(exc) from exc
    except RuntimeError as exc:
        raise _storage_error() from exc


@router.get("", summary="List your grief notes (newest first)")
async def list_notes(
    request: Request,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=200),
    date_from: Optional[date] = Query(default=None, alias="from"),
    date_to: Optional[date] = Query(default=None, alias="to"),
) -> Dict[str, Any]:
    principal: AccessTokenPayload = request.state.user
    try:
        return await service.list_notes(
            principal["user_id"], request.state.access_token,
            page=page, limit=limit, date_from=date_from, date_to=date_to,
        )
    except HeldApiError as exc:
        raise _held_error(exc) from exc
    except RuntimeError as exc:
        raise _storage_error() from exc


@router.post("", status_code=status.HTTP_201_CREATED, summary="Write a grief note for a date")
async def create_note(payload: GriefNoteCreate, request: Request) -> Dict[str, Any]:
    """One note per date (Held returns 409 for a second one). Mood by moodId or code; the
    optional exercise key records which workbook exercise the note answers."""
    principal: AccessTokenPayload = request.state.user
    try:
        mood_id = await _mood_id(payload.moodId, payload.mood)
        return await service.create_note(
            principal["user_id"], request.state.access_token, note_date=payload.date,
            content=payload.content, mood_id=mood_id, exercise_key=payload.exercise,
        )
    except HeldApiError as exc:
        raise _held_error(exc) from exc
    except RuntimeError as exc:
        raise _storage_error() from exc


@router.get("/{note_date}", summary="Get your grief note for a date")
async def get_note(note_date: date, request: Request) -> Dict[str, Any]:
    principal: AccessTokenPayload = request.state.user
    try:
        return await service.get_note(principal["user_id"], request.state.access_token, note_date)
    except HeldApiError as exc:
        raise _held_error(exc) from exc
    except RuntimeError as exc:
        raise _storage_error() from exc


@router.patch("/{note_date}", summary="Edit your grief note for a date")
async def update_note(note_date: date, payload: GriefNoteUpdate, request: Request) -> Dict[str, Any]:
    """content and mood are saved to Held; exercise (or null to clear it) is stored by the agent."""
    principal: AccessTokenPayload = request.state.user
    try:
        mood_id = await _mood_id(payload.moodId, payload.mood)
        return await service.update_note(
            principal["user_id"], request.state.access_token, note_date=note_date,
            content=payload.content, mood_id=mood_id, exercise_key=payload.exercise,
            change_exercise="exercise" in payload.model_fields_set,
        )
    except HeldApiError as exc:
        raise _held_error(exc) from exc
    except RuntimeError as exc:
        raise _storage_error() from exc
