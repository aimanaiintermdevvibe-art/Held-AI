import logging
from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, Request, status

from backend.db import repository
from backend.core.auth import AccessTokenPayload, require_auth
from backend.core.config import get_settings
from backend.schemas.sessions import (
    ChatSessionResponse,
    SessionDetailResponse,
)

logger = logging.getLogger("held_ai.api.sessions")

router = APIRouter(dependencies=[Depends(require_auth)])


# ==============================================================================
# Chat Session Management Endpoints
# ==============================================================================

@router.get(
    "/agentic/sessions",
    response_model=List[ChatSessionResponse],
    tags=["Sessions"],
    summary="List chat sessions",
)
@router.get(
    "/chat/sessions",
    response_model=List[ChatSessionResponse],
    tags=["Sessions"],
    include_in_schema=False,
)
async def get_sessions(
    request: Request,
) -> List[Dict[str, Any]]:
    """List the signed-in user's sessions (admins can inspect all sessions)."""
    principal: AccessTokenPayload = request.state.user
    user_id = None if principal["role"] == "ADMIN" else principal["user_id"]
    return await repository.list_chat_sessions(user_id=user_id)


@router.get(
    "/agentic/sessions/{session_id}/messages",
    response_model=SessionDetailResponse,
    tags=["Sessions"],
    summary="Get conversation history for a session",
)
@router.get(
    "/chat/sessions/{session_id}/messages",
    response_model=SessionDetailResponse,
    tags=["Sessions"],
    include_in_schema=False,
)
async def get_messages(session_id: str, request: Request) -> Dict[str, Any]:
    """Retrieve all messages for a session in chronological order."""
    principal: AccessTokenPayload = request.state.user
    owner_id = None if principal["role"] == "ADMIN" else principal["user_id"]
    session = await repository.get_chat_session(session_id, user_id=owner_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found.",
        )
    messages = await repository.get_session_messages(session_id)
    if not get_settings().show_token_usage:  # replies keep their token use under metadata.usage
        messages = [
            {**m, "metadata": {k: v for k, v in (m.get("metadata") or {}).items() if k != "usage"}}
            for m in messages
        ]
    return {
        "session": session,
        "messages": messages,
    }


@router.delete(
    "/agentic/sessions/{session_id}",
    tags=["Sessions"],
    summary="Delete a chat session",
)
@router.delete(
    "/chat/sessions/{session_id}",
    tags=["Sessions"],
    include_in_schema=False,
)
async def delete_session(session_id: str, request: Request) -> Dict[str, bool]:
    """Delete a session and cascading messages only when the caller owns it or is admin."""
    principal: AccessTokenPayload = request.state.user
    owner_id = None if principal["role"] == "ADMIN" else principal["user_id"]
    try:
        success = await repository.delete_chat_session(session_id, user_id=owner_id)
    except PermissionError as exc:  # the database user has no delete right
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found or could not be deleted.",
        )
    return {"success": True}
