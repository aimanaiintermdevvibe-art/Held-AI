"""Grief notes through Held's API (the source of truth), stored in public.grief_notes.

Every call uses the caller's own Held access token. Notes the agent creates, edits or
reads are stored locally with the workbook exercise they answer; listing syncs the
requested page from Held first and falls back to the stored copy if Held is unreachable.
Held has no delete for notes, so neither does the agent.
"""
import logging
import re
from datetime import date
from typing import Any

import httpx

from backend.core.auth import HELD_TIMEOUT_SECONDS
from backend.core.config import get_settings
from backend.core.held_client import HeldApiError
from backend.db import grief_notes as store
from backend.grief.exercises import exercise_summary

logger = logging.getLogger("held_ai.grief_notes")
HELD_PAGE_LIMIT = 200  # Held's maximum page size for GET /notes
CHAT_NOTES = 10
CHAT_NOTE_CHARS = 1000

# Messages about the user's own notes ("what did I write in my journal?") get their recent notes.
_REFERS_TO_NOTES = re.compile(
    r"\b(grief notes?|my notes?|notes? I|journal|diary|workbook|entr(?:y|ies)|"
    r"what (?:did|have) I (?:write|written|wrote))\b",
    re.IGNORECASE,
)


async def held(method: str, path: str, token: str, **kwargs: Any) -> Any:
    """Call Held's API as the caller; Held's errors are raised as HeldApiError with their code."""
    base_url = get_settings().held_api_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=HELD_TIMEOUT_SECONDS) as client:
            res = await client.request(method, f"{base_url}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs)
    except httpx.HTTPError as exc:
        raise HeldApiError(503, "The Held API could not be reached.", "HELD_UNAVAILABLE") from exc
    try:
        payload = res.json()
    except ValueError:
        payload = None
    if res.is_error or not isinstance(payload, dict):
        message = payload.get("message") if isinstance(payload, dict) else None
        code = payload.get("code") if isinstance(payload, dict) else None
        status = res.status_code if res.is_error else 502
        raise HeldApiError(status, message or f"Held API request failed ({res.status_code}).", code)
    return payload.get("data")


def present(note: dict[str, Any]) -> dict[str, Any]:
    """A stored note as returned to clients: Held's fields plus the workbook exercise."""
    return {**{k: v for k, v in note.items() if k != "exerciseKey"}, "exercise": exercise_summary(note.get("exerciseKey"))}


async def list_notes(
    user_id: str, token: str, *, page: int, limit: int, date_from: date | None, date_to: date | None,
) -> dict[str, Any]:
    """A page of the user's notes, newest first: synced from Held, served from the stored copy."""
    params: dict[str, Any] = {"page": page, "limit": limit}
    if date_from:
        params["from"] = date_from.isoformat()
    if date_to:
        params["to"] = date_to.isoformat()
    try:
        data = await held("GET", "/notes", token, params=params)
    except HeldApiError as exc:
        if exc.status_code != 503:
            raise
        logger.warning("Held unreachable; serving stored grief notes for %s.", user_id)
        notes, total = await store.list_notes(user_id, date_from, date_to, page, limit)
        pages = (total + limit - 1) // limit
        return {"notes": [present(n) for n in notes], "source": "stored copy (Held unreachable)",
                "pagination": {"page": page, "limit": limit, "total": total, "pages": pages,
                               "hasNext": page < pages, "hasPrev": page > 1}}

    held_notes = data.get("notes") or []
    await store.store_notes(user_id, held_notes)
    stored = {n["id"]: n for n in (await store.list_notes(user_id, date_from, date_to, 1, 10_000))[0]}
    return {
        "notes": [present(stored.get(n["id"], {**n, "exerciseKey": None})) for n in held_notes],
        "source": "held",
        "pagination": data.get("pagination"),
    }


async def sync_all(user_id: str, token: str) -> dict[str, int]:
    """Store every one of the user's Held notes (all pages)."""
    page, synced = 1, 0
    while True:
        data = await held("GET", "/notes", token, params={"page": page, "limit": HELD_PAGE_LIMIT})
        notes = data.get("notes") or []
        await store.store_notes(user_id, notes)
        synced += len(notes)
        if not (data.get("pagination") or {}).get("hasNext"):
            break
        page += 1
    _, total = await store.list_notes(user_id, limit=1)
    logger.info("Synced %s grief note(s) from Held for %s.", synced, user_id)
    return {"synced": synced, "stored": total}


async def get_note(user_id: str, token: str, note_date: date) -> dict[str, Any]:
    note = await held("GET", f"/notes/{note_date.isoformat()}", token)
    await store.store_notes(user_id, [note])
    return present(await store.get_note(user_id, note_date))


async def create_note(
    user_id: str, token: str, *, note_date: date, content: str, mood_id: str, exercise_key: str | None,
) -> dict[str, Any]:
    note = await held("POST", "/notes", token, json={"date": note_date.isoformat(), "content": content, "moodId": mood_id})
    await store.store_notes(user_id, [note], exercise_key)
    logger.info("Created grief note %s for %s.", note_date, user_id)
    return present(await store.get_note(user_id, note_date))


async def update_note(
    user_id: str, token: str, *, note_date: date, content: str | None, mood_id: str | None,
    exercise_key: str | None, change_exercise: bool,
) -> dict[str, Any]:
    """Edit content and/or mood on Held, and/or the workbook exercise (stored only by the agent)."""
    body = {k: v for k, v in (("content", content), ("moodId", mood_id)) if v is not None}
    note = await held("PATCH", f"/notes/{note_date.isoformat()}", token, json=body) if body \
        else await held("GET", f"/notes/{note_date.isoformat()}", token)
    await store.store_notes(user_id, [note])
    if change_exercise:
        await store.set_exercise(user_id, note_date, exercise_key)
    return present(await store.get_note(user_id, note_date))


async def notes_for_chat(user_id: str, token: str, prompt: str) -> list[dict[str, Any]]:
    """The user's recent notes when the message refers to them; [] otherwise. Best effort:
    synced from Held when possible, otherwise the stored copy, and [] if both fail."""
    if not _REFERS_TO_NOTES.search(prompt):
        return []
    try:
        await store.store_notes(user_id, (await held("GET", "/notes", token, params={"limit": CHAT_NOTES})).get("notes") or [])
    except (HeldApiError, ValueError) as exc:
        logger.warning("Could not sync grief notes for chat (%s); using the stored copy.", exc)
    try:
        notes, _ = await store.list_notes(user_id, limit=CHAT_NOTES)
    except RuntimeError as exc:
        logger.warning("Could not load grief notes for chat: %s", exc)
        return []
    return [
        {
            "date": n["date"],
            "mood": (n.get("mood") or {}).get("title"),
            **({"exercise": exercise_summary(n["exerciseKey"])["title"]} if exercise_summary(n.get("exerciseKey")) else {}),
            "text": n["content"][:CHAT_NOTE_CHARS],
        }
        for n in notes
    ]
