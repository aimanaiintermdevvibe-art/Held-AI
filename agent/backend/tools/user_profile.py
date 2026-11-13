"""Read-only user profile tool. Our code runs it (the model never chooses to) once per chat, when
the chat starts; the result is kept with the chat and reused by its later messages.

The same rules as the grief note tools:
- Identity: a UserProfileTool is bound to the user of the verified session token. It takes no
  user ID, username or name.
- Only "User", "UserDisease" and "Disease" are read (repository.get_user_profile).
- Only what picks the reply mode is returned: role, relation and the condition names. Never an
  email, a name or an ID.
- Logs never hold the conditions themselves, only how many there are.
"""
import logging
from typing import Any

from backend.db import repository

logger = logging.getLogger("held_ai.profile_tool")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

_MODE_FIELDS = ("role", "relation", "relationOther", "onboardingComplete")


def mode_fields(profile: Any) -> dict[str, Any] | None:
    """Only what picks the reply mode (role, relation, condition names); None for no profile."""
    if not isinstance(profile, dict) or not profile:
        return None
    kept: dict[str, Any] = {key: profile[key] for key in _MODE_FIELDS if profile.get(key) is not None}
    kept["diseases"] = [
        {"name": d["name"].strip()} for d in profile.get("diseases") or []
        if isinstance(d, dict) and isinstance(d.get("name"), str) and d["name"].strip()
    ]
    return kept


class UserProfileTool:
    """The profile tool for one request, bound to the signed-in user's ID (from the token)."""

    def __init__(self, user_id: str) -> None:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("UserProfileTool needs the authenticated user's ID")
        self._user_id = user_id.strip()

    async def run(self) -> dict[str, Any] | None:
        """The user's mode fields from the database, or None when it cannot give them."""
        try:
            profile = mode_fields(await repository.get_user_profile(self._user_id))
        except Exception as exc:  # unreachable, or no read access to the tables
            logger.warning("profile tool user=%s failed: %s", self._user_id, exc)
            return None
        logger.info("profile tool user=%s role=%s conditions=%d", self._user_id,
                    (profile or {}).get("role"), len((profile or {}).get("diseases") or []))
        return profile
