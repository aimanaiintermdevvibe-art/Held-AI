"""Local Held test accounts: one JSON file per user, chosen with HELD_ACTIVE_USER in .env.

Each file (users/user1.json, users/user2.json, ...) holds the account's credentials, the
Held API URL, its current access/refresh token pair and a copy of its last Held profile.
Held rotates the refresh token on every refresh and rejects a used one, so a user's pair
lives only in that user's file: every process signed in as the user reads and writes the
same file, refreshes are serialised across processes with a lock file beside it, and
writes are atomic.
"""
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[3]
USERS_DIR = ROOT_DIR / "users"
SELECTION_KEY = "HELD_ACTIVE_USER"
PROFILE_FIELDS = (
    "id", "email", "fullName", "username", "onboardingComplete", "role", "relation", "relationOther",
)


def user_file_for(selection: str) -> Path:
    """users/user<N>.json for a HELD_ACTIVE_USER of 1, 2, ... (a, b, ... also work)."""
    value = (selection or "").strip().lower()
    if len(value) == 1 and "a" <= value <= "z":
        value = str(ord(value) - ord("a") + 1)
    if not value.isdigit() or int(value) < 1:
        raise ValueError(f"{SELECTION_KEY} must be a user number such as 1 or 2 (got {selection!r}).")
    path = USERS_DIR / f"user{int(value)}.json"
    if not path.exists():
        raise FileNotFoundError(f"No account file for user {int(value)}: {path}")
    return path


class UserAccountFile:
    def __init__(self, path: Path, lock_timeout: float = 30.0, stale_lock_seconds: float = 60.0) -> None:
        self.path = Path(path)
        self.lock_file = self.path.with_name(f".{self.path.name}.lock")
        self.lock_timeout = lock_timeout
        self.stale_lock_seconds = stale_lock_seconds
        self._thread_lock = threading.RLock()
        self._depth = 0

    def read(self) -> dict[str, Any]:
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"{self.path} must contain a JSON object.")
        return data

    def update(self, **fields: Any) -> None:
        """Merge fields into the file, keeping everything else in it."""
        with self.lock():
            data = self.read()
            data.update(fields)
            self._write(data)

    # The token-store interface used by HeldAuthClient.
    def load(self) -> tuple[str, str]:
        data = self.read()
        return (data.get("access_token") or "").strip(), (data.get("refresh_token") or "").strip()

    def save(self, access_token: str, refresh_token: str) -> None:
        self.update(
            access_token=access_token,
            refresh_token=refresh_token,
            tokens_updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def credentials(self) -> tuple[str, str]:
        data = self.read()
        return (data.get("email") or "").strip(), data.get("password") or ""

    def remember_profile(self, profile: dict[str, Any]) -> None:
        """Keep a readable copy of the user's Held profile (the server always reads it live)."""
        snapshot = {key: profile.get(key) for key in PROFILE_FIELDS}
        snapshot["diseases"] = [
            d.get("name") for d in profile.get("diseases") or [] if isinstance(d, dict) and d.get("name")
        ]
        self.update(user_id=profile.get("id"), profile=snapshot)

    def _write(self, data: dict[str, Any]) -> None:
        temp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(temp, self.path)
                return
            except PermissionError:
                # Windows refuses the replace while another process briefly has the file open.
                if attempt == 4:
                    temp.unlink(missing_ok=True)
                    raise
                time.sleep(0.05 * (attempt + 1))

    @contextmanager
    def lock(self):
        """Re-entrant within a process, exclusive across processes."""
        with self._thread_lock:
            if self._depth == 0:
                self._acquire_file_lock()
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self.lock_file.unlink(missing_ok=True)

    def _acquire_file_lock(self) -> None:
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                os.close(os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return
            except FileExistsError:
                try:
                    if time.time() - self.lock_file.stat().st_mtime > self.stale_lock_seconds:
                        self.lock_file.unlink(missing_ok=True)  # left behind by a crashed process
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Timed out waiting for another process to update {self.path.name}.")
                time.sleep(0.1)
