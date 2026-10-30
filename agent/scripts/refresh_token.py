"""Get a fresh Held access token for the selected account and copy it into .env.

    python agent/scripts/refresh_token.py [--user 2]

Uses HELD_ACTIVE_USER from .env unless --user is given. The account's refresh token is
exchanged for a new pair (logging in with the account's password if the refresh token no
longer works), the pair is saved to users/user<N>.json as usual, and HELD_ACCESS_TOKEN /
HELD_REFRESH_TOKEN in the root .env are set to it, ready to paste into Postman. The .env
copy is a snapshot: the terminal chat keeps using and rotating the pair in the account file.
"""
import argparse
import codecs
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import jwt

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))

from backend.core.held_client import HeldApiError, HeldAuthClient  # noqa: E402
from backend.core.user_accounts import SELECTION_KEY, UserAccountFile, user_file_for  # noqa: E402
from chat import _setting  # noqa: E402

ENV_FILE = ROOT / ".env"


def set_env_values(path: Path, values: dict[str, str]) -> None:
    """Replace KEY=... lines (adding missing keys), keeping every other line, comment and line ending."""
    raw = path.read_bytes() if path.exists() else b""
    bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    pending = dict(values)
    for i, line in enumerate(lines):
        for key in list(pending):
            if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                lines[i] = f"{key}={pending.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in pending.items())
    path.write_bytes(bom + (newline.join(lines) + newline).encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", help=f"account number (default: {SELECTION_KEY} in .env)")
    args = parser.parse_args()

    account = UserAccountFile(user_file_for(args.user or _setting(SELECTION_KEY, "1")))
    held = HeldAuthClient(account)
    try:
        held.refresh() if held.refresh_token else held.login()
    except HeldApiError as exc:
        if exc.status_code not in (400, 401, 403):
            raise
        print(f"Refresh token was not accepted ({exc.code}); logging in with the account's password.")
        held.login()
    profile = held.sign_in()  # confirms the token works and belongs to this account
    account.remember_profile(profile)

    set_env_values(ENV_FILE, {"HELD_ACCESS_TOKEN": held.access_token, "HELD_REFRESH_TOKEN": held.refresh_token})
    expires = jwt.decode(held.access_token, options={"verify_signature": False}).get("exp", time.time())
    print(f"Refreshed {account.path.name}: {profile.get('fullName')} ({profile.get('email')}).")
    print(f"HELD_ACCESS_TOKEN and HELD_REFRESH_TOKEN updated in {ENV_FILE}.")
    print(f"The access token is valid until {datetime.fromtimestamp(expires):%H:%M:%S} "
          f"({int((expires - time.time()) // 60)} minutes). Run this again for a new one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
