import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from backend.db import repository
from backend.core.auth import AccessTokenPayload, require_auth
from backend.schemas.sessions import (
    ChatSessionResponse,
    ChatMessageResponse,
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
    success = await repository.delete_chat_session(session_id, user_id=owner_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found or could not be deleted.",
        )
    return {"success": True}


# ==============================================================================
# User, Disease, and Overview Endpoints
# ==============================================================================

@router.get(
    "/agentic/users",
    tags=["Users"],
    summary="List registered users (read-only)",
)
async def list_users_endpoint(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
) -> List[Dict[str, Any]]:
    """Retrieve users from public."User" (read-only view). User creation is handled by Node.js."""
    _require_admin(request)
    return await repository.list_users(limit=limit)


@router.post(
    "/agentic/users/register",
    tags=["Users"],
    summary="Register the signed-in Held user with the agent",
)
async def register_user_endpoint(request: Request) -> Dict[str, Any]:
    """Return the caller's public."User" row, adding it from their Held profile if missing.

    Clients call this after Held sign-up and onboarding (and on sign-in), so the user
    exists in the agent's database before their first chat. Authentication does the
    adding; existing rows are never changed.
    """
    principal: AccessTokenPayload = request.state.user
    user = await repository.get_user_by_id(principal["user_id"])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not register the user. Please try again.",
        )
    return user


@router.get(
    "/agentic/users/{user_id}",
    tags=["Users"],
    summary="Get user profile by ID (read-only)",
)
async def get_user_endpoint(user_id: str, request: Request) -> Dict[str, Any]:
    """Retrieve single user details from public."User" (read-only view)."""
    _require_admin(request)
    user = await repository.get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found.",
        )
    return user


@router.get(
    "/agentic/diseases",
    tags=["Diseases"],
    summary="List disease categories",
)
async def list_diseases_endpoint() -> List[Dict[str, Any]]:
    """The disease catalogue seeded from Held's GET /diseases (scripts/seed_diseases.py)."""
    try:
        return await repository.list_diseases()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The disease catalogue is unavailable.",
        ) from exc


@router.get(
    "/agentic/admin/overview",
    tags=["Admin"],
    summary="Database entity counts overview",
)
async def admin_overview_endpoint(request: Request) -> Dict[str, Any]:
    _require_admin(request)
    return await repository.get_database_overview()


def _require_admin(request: Request) -> None:
    principal: AccessTokenPayload = request.state.user
    if principal["role"] != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")