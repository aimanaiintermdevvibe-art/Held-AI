import logging
import time
from typing import Any, Dict, Literal, TypedDict

import httpx
import jwt
from fastapi import HTTPException, Request, status

from backend.core.config import get_settings
from backend.db import repository

logger = logging.getLogger("held_ai.auth")
logger.setLevel(logging.INFO)
if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console_handler)
logger.propagate = False


class AccessTokenPayload(TypedDict):
    user_id: str
    role: Literal["ADMIN", "CAREGIVER", "SELF"]


class HeldUnavailable(RuntimeError):
    """The Held API could not be reached or gave an unusable answer."""


# Held answers GET /auth/me in about 2-3 s and sometimes slower, so a token Held has
# confirmed is trusted for a short while instead of asking again on every request.
HELD_TIMEOUT_SECONDS = 10.0
CONFIRMED_TOKEN_TTL_SECONDS = 60.0
_CONFIRMED_TOKENS_MAX = 1000
_confirmed_tokens: Dict[str, tuple[float, str, Dict[str, Any]]] = {}  # token -> (expires, user_id, profile)

# With AUTH_DISABLED=true every request is this one user.
GUEST_PROFILE = {"id": "held-ai-guest", "email": "guest@held-ai.invalid", "fullName": "Test Guest", "role": "SELF"}


def _remember_confirmed(token: str, user_id: str, profile: Dict[str, Any], token_expires_at: Any) -> None:
    expires = time.monotonic() + CONFIRMED_TOKEN_TTL_SECONDS
    if isinstance(token_expires_at, (int, float)):
        expires = min(expires, time.monotonic() + (token_expires_at - time.time()))
    if len(_confirmed_tokens) >= _CONFIRMED_TOKENS_MAX:
        _confirmed_tokens.pop(next(iter(_confirmed_tokens)))  # oldest first
    _confirmed_tokens[token] = (expires, user_id, profile)


def _recently_confirmed(token: str) -> tuple[str, Dict[str, Any]] | None:
    entry = _confirmed_tokens.get(token)
    if not entry:
        return None
    expires, user_id, profile = entry
    if expires <= time.monotonic():
        _confirmed_tokens.pop(token, None)
        return None
    return user_id, profile


async def held_me(access_token: str) -> Dict[str, Any] | None:
    """Held's view of an access token: the caller's profile from GET /auth/me, or None
    when Held rejects the token. Raises HeldUnavailable when Held cannot answer."""
    base_url = get_settings().held_api_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=HELD_TIMEOUT_SECONDS) as client:
            res = await client.get(f"{base_url}/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as exc:
        raise HeldUnavailable(str(exc) or type(exc).__name__) from exc
    if res.status_code in (401, 403):
        return None
    if res.is_error:
        raise HeldUnavailable(f"Held /auth/me returned {res.status_code}")
    try:
        payload = res.json()
    except ValueError as exc:
        raise HeldUnavailable("Held profile response is not JSON.") from exc
    profile = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(profile, dict):
        raise HeldUnavailable("Held profile response data must be a JSON object.")
    return profile


async def fetch_user_profile(access_token: str, user_id: str | None = None) -> Dict[str, Any]:
    """The caller's Held profile (role, relation, diseases) from GET /auth/me.

    Uses the caller's own Held access token, so the agent holds no Held credentials and
    never competes with the client for its rotating refresh token. Returns {} when the
    profile is unavailable; replies are then written without profile context.
    """
    try:
        profile = await held_me(access_token)
    except HeldUnavailable as exc:
        logger.warning("Could not fetch Held profile for user %s: %s", user_id, exc)
        return {}
    if profile is None:
        logger.warning("Held rejected the access token while fetching the profile for user %s.", user_id)
        return {}
    if user_id and profile.get("id") not in (None, user_id):
        logger.warning("Held profile for user %s belongs to a different user.", user_id)
        return {}
    return profile


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _subject(claims: Dict[str, Any]) -> str:
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise jwt.InvalidTokenError("malformed payload")
    return user_id


async def _authenticate(token: str) -> tuple[str, Dict[str, Any] | None]:
    """The token's user id, plus Held's profile when Held had to confirm the token.

    A token signed with the shared JWT_SECRET is verified locally. A token signed with a
    key this server does not have (for example after the Held API rotates its secret) is
    accepted only if Held itself confirms it through GET /auth/me.
    """
    secret = get_settings().jwt_secret
    if secret:
        try:
            return _subject(jwt.decode(token, secret, algorithms=["HS256"])), None
        except jwt.ExpiredSignatureError as exc:
            raise _unauthorized("TOKEN_EXPIRED", "Access token expired") from exc
        except jwt.InvalidSignatureError:
            pass
        except jwt.InvalidTokenError as exc:
            raise _unauthorized("TOKEN_INVALID", "Invalid access token") from exc

    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        user_id = _subject(claims)
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("TOKEN_INVALID", "Invalid access token") from exc
    expires_at = claims.get("exp")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        raise _unauthorized("TOKEN_EXPIRED", "Access token expired")

    confirmed = _recently_confirmed(token)
    if confirmed:
        return confirmed
    try:
        profile = await held_me(token)
    except HeldUnavailable as exc:
        logger.warning("Could not confirm an access token with Held: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable.",
        ) from exc
    if not profile or profile.get("id") != user_id:
        raise _unauthorized("TOKEN_INVALID", "Invalid access token")
    _remember_confirmed(token, user_id, profile, expires_at)
    return user_id, profile


async def _authenticate_guest(request: Request) -> AccessTokenPayload:
    """The shared guest user for AUTH_DISABLED, added to public."User" on first use.

    The role is fixed rather than read from the row, so the guest can never reach admin routes.
    """
    try:
        user = await repository.get_auth_user(GUEST_PROFILE["id"])
        if not user:
            user = await repository.create_user_from_profile(GUEST_PROFILE)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The guest user is unavailable.",
        ) from exc
    if not user:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The guest user is unavailable.",
        )

    principal: AccessTokenPayload = {"user_id": user["id"], "role": "SELF"}
    request.state.user = principal
    request.state.access_token = ""
    request.state.held_profile = {}  # no Held account, so replies are written without profile context
    return principal


async def require_auth(request: Request) -> AccessTokenPayload:
    """FastAPI authentication dependency verifying Held bearer tokens.

    A Held user missing from public."User" (for example, one who just registered) is
    added from their Held profile, so the agent can store their conversations.
    With AUTH_DISABLED, any token sent is ignored and the caller is the guest user.
    """
    if get_settings().auth_disabled:
        return await _authenticate_guest(request)

    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "TOKEN_MISSING",
                "message": "Send a Held-issued access token as Authorization: Bearer <token>.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = header[len("Bearer "):].strip()
    user_id, profile = await _authenticate(token)

    try:
        user = await repository.get_auth_user(user_id)
        if not user:
            if profile is None:
                profile = await fetch_user_profile(token, user_id)
            if profile.get("id") == user_id:
                user = await repository.create_user_from_profile(profile)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable.",
        ) from exc

    if not user:
        raise _unauthorized("USER_NOT_FOUND", "Token subject is not a Held user")

    role = user.get("role")
    principal: AccessTokenPayload = {
        "user_id": user["id"],
        "role": role if role in ("ADMIN", "CAREGIVER") else "SELF",
    }
    request.state.user = principal
    request.state.access_token = token
    request.state.held_profile = profile  # reused for this request's replies when present
    return principal
