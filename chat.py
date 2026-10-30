import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import httpx

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent"))
from backend.ai_wrapper.profile import describe, user_context  # noqa: E402
from backend.core.held_client import HeldApiError, HeldAuthClient  # noqa: E402
from backend.core.user_accounts import SELECTION_KEY, UserAccountFile, user_file_for  # noqa: E402


def _setting(name: str, default: str = "") -> str:
    """Read a setting from the process environment, then the project .env."""
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()

    env_path = Path(__file__).resolve().with_name(".env")
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return default

    for line in lines:
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        key, separator, raw_value = candidate.partition("=")
        if separator and key.strip() == name:
            result = raw_value.strip()
            if len(result) >= 2 and result[0] == result[-1] and result[0] in "\"'":
                result = result[1:-1]
            return result or default
    return default


API_URL = _setting(
    "AGENTIC_STREAM_URL",
    _setting("API_URL", "https://held-ai-chatbot.onrender.com/agentic/stream"),
)
AGENT_BASE_URL = API_URL.rsplit("/agentic/", 1)[0]
TIMEOUT_SECONDS = 300

# The selected account's Held session (set by sign_in). Its tokens live in that account's
# users/user<N>.json, so a refresh here is seen by every other run and process.
held: HeldAuthClient | None = None

CURRENT_SESSION_ID: str | None = None


class TokenRejected(RuntimeError):
    """The agent API refused the access token."""


def sign_in(selection: str | None = None) -> dict:
    """Sign in the account chosen by HELD_ACTIVE_USER and return its Held profile.

    The saved token is used while valid, refreshed when it expires, and the account's
    email and password are used to log in only when the refresh token no longer works.
    """
    global held
    account = UserAccountFile(user_file_for(selection or _setting(SELECTION_KEY, "1")))
    held = HeldAuthClient(account)
    profile = held.sign_in()
    account.remember_profile(profile)
    return profile


# ---------------------------------------------------------------------------
# Agent API
# ---------------------------------------------------------------------------

def register_with_agent() -> None:
    """Make sure the agent's database has this Held user; the agent adds a new user's row."""
    token = held.get_valid_token()
    for attempt in range(2):
        request = urllib.request.Request(
            f"{AGENT_BASE_URL}/agentic/users/register",
            data=b"",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response.read()
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and attempt == 0:
                token = held.refresh_or_login(rejected_token=token)
                continue
            print(f"[Could not register with the agent: HTTP {exc.code}: {detail}]")
            return
        except urllib.error.URLError as exc:
            print(f"[Could not reach the agent API: {exc.reason}]")
            return


def ask_agent(prompt: str) -> str:
    """Stream the agent response; an expired token is refreshed and the request retried once."""
    token = held.get_valid_token()
    try:
        return _stream_answer(prompt, token)
    except TokenRejected:
        return _stream_answer(prompt, held.refresh_or_login(rejected_token=token))


def _stream_answer(prompt: str, access_token: str) -> str:
    """Stream the agent response to the terminal and return the full text."""
    global CURRENT_SESSION_ID
    request_body = {"prompt": prompt.strip()}
    if CURRENT_SESSION_ID:
        request_body["session_id"] = CURRENT_SESSION_ID

    payload = json.dumps(request_body).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            **({"Authorization": f"Bearer {access_token}"} if access_token else {}),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    answer: list[str] = []
    data_lines: list[str] = []

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            print("\nAgent: ", end="", flush=True)
            for raw_line in response:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data_lines:
                        event_data = json.loads("\n".join(data_lines))
                        sess_obj = event_data.get("session") or {}
                        sid = event_data.get("session_id") or event_data.get("sessionId") or sess_obj.get("id")
                        if sid and not CURRENT_SESSION_ID:
                            CURRENT_SESSION_ID = sid

                        if "text" in event_data:
                            text = event_data["text"]
                            answer.append(text)
                            print(text, end="", flush=True)
                        elif event_data.get("done"):
                            print()
                            return "".join(answer)
                        elif "detail" in event_data:
                            print()
                            if event_data.get("session_discarded"):
                                # The server deleted the empty chat, so the next message must start a new one.
                                CURRENT_SESSION_ID = None
                            raise RuntimeError(event_data["detail"])
                    data_lines = []
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise TokenRejected(f"Your sign-in was not accepted ({detail}).") from exc
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach API: {exc.reason}") from exc

    print()
    raise RuntimeError("The stream ended before a done event was received.")


def handle_prompt(prompt: str) -> None:
    try:
        ask_agent(prompt)
    except (RuntimeError, ValueError, httpx.HTTPError) as exc:
        print(f"\n[Error] {exc}")


def _print_signed_in(profile: dict) -> None:
    label = held.store.read().get("label") or held.store.path.stem
    print(f" {label}: {profile.get('fullName') or profile.get('email')} ({profile.get('email')})")
    print(f" Mode: {describe(user_context(profile))}")
    if not profile.get("onboardingComplete"):
        print(" This account hasn't finished onboarding in the Held app, so replies use no mode.")


def main() -> None:
    global CURRENT_SESSION_ID
    try:
        profile = sign_in()
    except (FileNotFoundError, ValueError) as exc:
        print(f"[Error] {exc}")
        return
    except (HeldApiError, httpx.HTTPError) as exc:
        print(f"[Error] Could not sign in the selected user: {exc}")
        return
    register_with_agent()

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        print(f"\nYou: {prompt}")
        handle_prompt(prompt)
        return

    print("=" * 60)
    print(" Held AI Agent - Streaming Terminal Chat")
    _print_signed_in(profile)
    print(f" To switch user, set {SELECTION_KEY}=1 or 2 in .env and restart.")
    print(" Commands: 'new' starts a new chat, 'profile' shows your mode,")
    print(" 'exit' or 'quit' ends.")
    print("=" * 60)

    while True:
        try:
            prompt = input("\nYou: ").strip()
            if not prompt:
                continue
            command = prompt.lower()
            if command in ("exit", "quit", "q"):
                print("Goodbye!")
                break
            if command == "new":
                CURRENT_SESSION_ID = None
                print("[Started a new chat - earlier chats are still remembered]")
                continue
            if command == "profile":
                profile = held.me()
                held.store.remember_profile(profile)
                _print_signed_in(profile)
                continue
            handle_prompt(prompt)
        except (HeldApiError, httpx.HTTPError) as exc:
            print(f"\n[Error] {exc}")
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break


if __name__ == "__main__":
    main()
