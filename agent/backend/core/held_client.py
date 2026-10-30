"""Held API client for one local account file: login, token refresh and the user's profile.

Every Held call goes through one token workflow: use the stored access token until it is
about to expire, then rotate it with the refresh token, and log in with the account's
email and password only as a fallback. The rotated pair is written back to the account
file, so every process and every later run continues from the latest refresh token
instead of replaying a used one.
"""
import logging
import time
from typing import Any

import httpx
import jwt

from backend.core.config import Settings, get_settings
from backend.core.user_accounts import UserAccountFile

logger = logging.getLogger("held_ai.held_client")
TOKEN_REFRESH_EARLY_SECONDS = 60


class HeldApiError(RuntimeError):
    """A Held API error response ({"success": false, "code": ..., "message": ...})."""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _data(res: httpx.Response) -> Any:
    try:
        payload = res.json()
    except ValueError:
        payload = None
    if res.is_error or not isinstance(payload, dict):
        message = payload.get("message") if isinstance(payload, dict) else None
        code = payload.get("code") if isinstance(payload, dict) else None
        raise HeldApiError(res.status_code, message or f"Held API request failed ({res.status_code}).", code)
    return payload.get("data")


class HeldAuthClient:
    """Signs in the account in one account file and calls the Held API as that user."""

    def __init__(
        self,
        account: UserAccountFile,
        http: httpx.Client | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.store = account
        self.http = http or httpx.Client(timeout=15.0)
        base_url = account.read().get("held_api_base_url") or (settings or get_settings()).held_api_base_url
        self.base_url = base_url.rstrip("/")
        self.access_token, self.refresh_token = self.store.load()

    # -- token workflow -------------------------------------------------------

    def get_valid_token(self) -> str:
        """Return a usable access token, refreshing (or logging in) only when needed."""
        if self._usable(self.access_token):
            return self.access_token
        with self.store.lock():
            self._reload()  # another process may have rotated the pair while we waited
            if self._usable(self.access_token):
                return self.access_token
            if self.refresh_token:
                return self.refresh_or_login()
            return self.login()

    def refresh_or_login(self, rejected_token: str | None = None) -> str:
        """Refresh with the rotating token, falling back to login if it is rejected.

        rejected_token is an access token the API just refused; if another process has
        already replaced it, the replacement is used instead of rotating again.
        """
        with self.store.lock():
            self._reload()
            if rejected_token and self.access_token != rejected_token and self._usable(self.access_token):
                return self.access_token
            if not self.refresh_token:
                return self.login()
            try:
                return self.refresh()
            except HeldApiError as exc:
                if exc.status_code not in (400, 401, 403):
                    raise
                logger.warning("Held refresh token was rejected (%s); logging in again.", exc.code)
                self._save("", "")
                return self.login()

    def refresh(self) -> str:
        """Exchange the current refresh token for a rotated access/refresh token pair."""
        with self.store.lock():
            if not self.refresh_token:
                raise ValueError("Held refresh token is not configured.")
            logger.info("Refreshing Held access and refresh tokens.")
            res = self.http.post(f"{self.base_url}/auth/refresh", json={"refreshToken": self.refresh_token})
            return self._update_tokens(_data(res), require_refresh_token=True)

    def login(self) -> str:
        """Log in with the email and password in the account file."""
        email, password = self.store.credentials()
        if not email or not password:
            logger.warning("%s has no email/password; cannot log in.", self.store.path.name)
            self.access_token = ""
            return ""
        with self.store.lock():
            logger.info("Logging in %s.", email)
            res = self.http.post(f"{self.base_url}/auth/login", json={"email": email, "password": password})
            return self._update_tokens(_data(res))

    @staticmethod
    def _usable(token: str) -> bool:
        if not token:
            return False
        try:
            claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
        except jwt.PyJWTError:
            return False
        expires_at = claims.get("exp")
        return not (isinstance(expires_at, (int, float)) and expires_at <= time.time() + TOKEN_REFRESH_EARLY_SECONDS)

    def _reload(self) -> None:
        self.access_token, self.refresh_token = self.store.load()

    def _save(self, access_token: str, refresh_token: str) -> None:
        self.access_token, self.refresh_token = access_token, refresh_token
        self.store.save(access_token, refresh_token)

    def _update_tokens(self, data: Any, *, require_refresh_token: bool = False) -> str:
        if not isinstance(data, dict):
            raise ValueError("Held auth response data must be a JSON object.")
        access_token = data.get("accessToken")
        refresh_token = data.get("refreshToken")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Held auth response did not include an access token.")
        if require_refresh_token and (not isinstance(refresh_token, str) or not refresh_token):
            raise ValueError("Held refresh response did not include a rotated refresh token.")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = self.refresh_token
            logger.warning("Held returned no refresh token; automatic refresh may require another login.")
        self._save(access_token, refresh_token)
        return access_token

    # -- authenticated requests -----------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Call the Held API as the signed-in user; an expired token is refreshed and retried once."""
        token = self.get_valid_token()
        res = self._send(method, path, token, **kwargs)
        if res.status_code == 401 and token:
            token = self.refresh_or_login(rejected_token=token)
            res = self._send(method, path, token, **kwargs)
        return _data(res)

    def _send(self, method: str, path: str, token: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.http.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)

    def me(self) -> dict:
        """The signed-in user's profile, including role, relation and diseases."""
        return self.request("GET", "/auth/me")

    def sign_in(self) -> dict:
        """The account's profile, signing in as needed.

        Saved tokens that belong to a different account than the file's email (for example,
        a pair copied from elsewhere) are discarded, and the account logs in with its own
        credentials.
        """
        profile = self.me()
        email, _ = self.store.credentials()
        if email and str(profile.get("email") or "").lower() != email.lower():
            logger.warning(
                "Saved tokens in %s belong to %s, not %s; logging in again.",
                self.store.path.name, profile.get("email"), email,
            )
            with self.store.lock():
                self._save("", "")
            profile = self.me()
            if str(profile.get("email") or "").lower() != email.lower():
                raise HeldApiError(401, f"Could not sign in as {email}.", "ACCOUNT_MISMATCH")
        return profile
