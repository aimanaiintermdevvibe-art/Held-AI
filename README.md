# Held AI Backend

Held AI is the chat backend for the Held app. It gives mental-health, caregiving and grief support, with replies written by Google Gemini. It is a Python FastAPI service that works on the Held app's own PostgreSQL database and calls the Held API to confirm sign-ins and, as a fallback, to read profiles.

## How a message is answered

1. An instant word check catches clear crisis messages.
2. Agent 1 picks the route: crisis, grief, clinical question, general support or off-topic.
3. Agent 2 adds clinical or grief reasoning, for those two routes only.
4. Agent 3 writes the reply in plain, human language and avoids stock phrases.
5. Agent 4 reviews it for safety.

The turn is saved in one database write.

**Memory:**

- **Within a chat:** the latest 20 messages, plus a rolling summary of older ones that is updated every 10 messages.
- **Across chats:** a new chat starts fresh and does not bring up earlier chats. When the user refers back to one, the agent searches their earlier chats (last 365 days). A short user memory is kept as background, and an open safety concern is checked on once per account.

Each agent receives only the part of the memory it needs, so a message costs about the same however long the chat gets.

## Setup

You need Python 3.12 or later and access to the Held database.

**1. Configure.** Copy `agent/.env.example` to `agent/.env` and fill it in.

| Setting | Value |
| --- | --- |
| `DATABASE_URL` | The Held PostgreSQL database. Write a `@` in the password as `%40` |
| `GEMINI_API_KEY` | Google Gemini API key |
| `HELD_API_BASE_URL` | Held API, for example `http://held-api-host:4000/api` |
| `JWT_SECRET` | The key Held signs access tokens with. Optional: without it, each token is confirmed with Held |

**2. Install.** There are no migrations. The Held app owns the tables.

```powershell
cd agent
python -m pip install -r requirements-dev.txt
```

**3. Test.**

```powershell
python -m pytest -q
```

All 378 tests should pass. They use an in-memory stand-in, so they need neither the database nor Gemini.

**4. Run.**

```powershell
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

`python -m uvicorn main:app` also works from the repository root.

## Database

The service uses the Held app's tables (Prisma-managed) and creates none of its own.

| Table | Use | Access needed |
| --- | --- | --- |
| `ChatSession` | One row per chat. Holds the title, `sessionSummary`, `lastSummarizedMessageId`, `totalMessageCount`, and the user memory in `metadata` | select, insert, update, delete |
| `ChatMessage` | Every message and reply | select, insert, update, delete |
| `User` | Signed-in users must exist here. This service never adds users. Role and relation pick the reply mode | select |
| `UserDisease`, `Disease` | The conditions a user chose at onboarding, which pick the reply mode | select |
| `GriefNote` | Users' grief notes, read by the grief note tools in chat | select |
| `Mood` | Mood titles for the grief note tools | select |

- **Reply modes:** the user's role (themselves or a caregiver) and conditions pick one of four modes: myself or caregiver, each with or without a diagnosis. They are read from `User`, `UserDisease` and `Disease`; while those cannot be read, they come from Held's `GET /auth/me`.
- **User profile:** the reply mode comes from the user's role, relation and conditions, read through a read-only profile tool (`backend/tools/user_profile.py`) bound to the token's user. It is read once when a chat starts, kept with the chat (hidden from the app) and reused by that chat's later messages; a new chat reads it afresh.
- **Grief note tools:** in a chat, the writer (Agent 3) may call three read-only tools when the answer depends on the user's notes: `recent_notes` (days back, most notes), `note_by_date` (one date) and `mood_summary` (note count and mood frequencies by week). The user comes only from the session token (no tool takes a user ID); arguments are validated and clamped (90 days, 20 notes, 1,500 characters per note, 4 calls per message); unknown tools and malformed arguments are rejected. Results reach the writer and the auditor as user-written data, never instructions; crisis language in a note takes the crisis path. Dates are UTC. Each call is logged without the note text.
- **Deleting a chat:** removes the chat, and the database's cascade removes its messages. If the database user has no delete right, the endpoint answers 403.
- **Timestamps:** times are stored in UTC, with millisecond precision. A reply is stamped 1 ms after its message, so the order is kept.

Each chat message costs one database read and one write. Every 10 messages, the background summary adds one small read and one write.

## API

Every endpoint except `/` and `/agentic/health` needs `Authorization: Bearer <Held access token>`. The token's user must exist in `User`; otherwise the answer is 401 `USER_NOT_FOUND`.

| Endpoint | Purpose |
| --- | --- |
| `GET /`, `GET /agentic/health` | Service check. Health also reports the database |
| `POST /agentic/run` | Send a message and get the reply as JSON |
| `POST /agentic/stream` | The same, streamed as server-sent events |
| `GET /agentic/sessions` | The user's chats |
| `GET /agentic/sessions/{id}/messages` | One chat's messages |
| `DELETE /agentic/sessions/{id}` | Delete a chat and its messages |

Example request:

```json
POST /agentic/run
{ "prompt": "I've been feeling stressed at work.", "session_id": "<leave out to start a new chat>" }
```

To send a grief note the user wrote in the app, add `"isNote": true` (default `false`). Each note starts a new chat; the chat and the note's message are marked `isNote` in their `metadata`, and the agent replies to it as a grief companion (crisis language still takes the crisis path).

The response is `{"response": "...", "session_id": "...", "session": {...}}`. Send the `session_id` with the next message to continue the chat.

## Code layout

| Path | Contents |
| --- | --- |
| `agent/backend/main.py` | App entry point |
| `agent/backend/api/routers/` | Endpoints |
| `agent/backend/ai_wrapper/` | The four agents and their prompts, the Gemini client, memory and token logging |
| `agent/backend/db/` | Connection pool and queries on the Held tables |
| `agent/backend/core/` | Settings and authentication |
| `agent/backend/grief/` | Grief note tools: read-only lookups of the user's own notes during a chat |
| `agent/tests/` | Unit tests |

## Good to know

- **Gemini calls** are retried on temporary failures, and each answer is capped at 8,192 tokens.
- **Token logging:** every reply logs a `usage turn ...` line with its tokens and estimated cost.
- **Crisis safety:** crisis replies keep their safety wording, and the instant crisis check runs on every message.
