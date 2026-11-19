# Held AI Agent API

As of 2026-11-19.

The Held AI Agent API answers chat messages with a four-agent Gemini pipeline and saves each conversation in the Held app's own PostgreSQL tables (`ChatSession`, `ChatMessage`).

## Base URLs

| Environment | URL |
| :--- | :--- |
| Production (Render) | `https://held-ai.onrender.com` |
| Local | `http://127.0.0.1:8000` (or the port you start it on) |

Swagger is served at `/docs` only when `SHOW_API_DOCS=true`, for local testing. It is off on Render.

## Endpoints

| Method | Endpoint | Purpose | Token |
| :--- | :--- | :--- | :--- |
| `POST` | `/agentic/stream` | Send a message; the reply comes as server-sent events | Yes |
| `POST` | `/agentic/run` | Send a message; the reply comes as JSON | Yes |
| `GET` | `/agentic/sessions` | The user's chats | Yes |
| `GET` | `/agentic/sessions/{id}/messages` | One chat and its messages | Yes |
| `DELETE` | `/agentic/sessions/{id}` | Delete a chat and its messages | Yes |
| `GET` | `/agentic/health` | Service and database check | No |
| `GET` | `/` | Liveness check; touches no database | No |

There are no other endpoints. User, disease, admin, document and grief-note endpoints were removed: grief notes are read by the agent's own tools during a chat (see [Grief notes in chat](#grief-notes-in-chat)).

## Authentication

Every endpoint except `/` and `/agentic/health` needs a Held access token:

```http
Authorization: Bearer <held_access_token>
```

- **Verification:** a token signed with the server's `JWT_SECRET` is verified locally, with no call to Held. A token signed with another key is accepted only if Held confirms it through `GET {HELD_API_BASE_URL}/auth/me`; a confirmation is reused for 60 seconds.
- **User:** the token's `sub` must be the `id` of a row in the Held `User` table. This service never creates users.
- **Ownership:** a user sees only their own chats. A `user_id` sent in the body is ignored. A user whose `User.role` is `ADMIN` can open any chat.
- **Expiry:** Held access tokens last 15 minutes. Get a new one from Held's login or refresh.

| Status | `detail.code` | Meaning |
| :--- | :--- | :--- |
| `401` | `TOKEN_MISSING` | No `Authorization: Bearer` header |
| `401` | `TOKEN_INVALID` | Malformed, tampered, or not confirmed by Held |
| `401` | `TOKEN_EXPIRED` | The token has expired |
| `401` | `USER_NOT_FOUND` | The token's user is not in the `User` table |
| `503` | (text) | The token could not be checked: `JWT_SECRET` does not match and Held could not be reached |

## Sending a message

`POST /agentic/run` and `POST /agentic/stream` take the same body.

```json
{
  "prompt": "I feel overwhelmed and need help taking one small step.",
  "session_id": "6f0c7c8e-1c2d-4c1e-9a55-0f6a1d2b3c4d"
}
```

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `prompt` | string | Yes | 1 to 10,000 characters, trimmed |
| `session_id` | string | No | The chat to continue. Leave it out to start a new chat |
| `isNote` | boolean | No | Default `false`. `true` when `prompt` is a grief note the user wrote in the app (see below) |
| `metadata` | object | No | Custom data stored on a new chat. The `memory` key is reserved and dropped |
| `title` | string | No | Ignored: the AI names each new chat |
| `user_id` | string | No | Ignored: the user comes from the token |
| `session`, `session_data` | object | No | Older nested form; `id` and `metadata` inside are read as above |

Any other field is rejected with `422`. A `422` says which field is wrong and why, but never repeats what was sent, because a prompt can hold a sensitive disclosure.

### A grief note sent from the app (`isNote`)

With `"isNote": true` the agent replies to the note as a grief companion.

- Each note starts its own new chat; a `session_id` is ignored.
- The chat and the note's message are marked `"isNote": true` in their `metadata`.
- Crisis language in the note still gets a crisis reply.
- Follow-up messages in that chat are sent without `isNote`, with the chat's `session_id`.
- The note's mood reaches the agent only if the app writes it into `prompt`, for example `Mood: Sad`.

### `POST /agentic/run`

```json
{
  "response": "A week like that leaves very little room to breathe...",
  "session_id": "6f0c7c8e-1c2d-4c1e-9a55-0f6a1d2b3c4d",
  "session": {
    "id": "6f0c7c8e-1c2d-4c1e-9a55-0f6a1d2b3c4d",
    "user_id": "usr_...",
    "title": "Stressful week at work",
    "status": "ACTIVE",
    "metadata": {},
    "session_summary": null,
    "last_summarized_message_id": null,
    "total_message_count": 2,
    "created_at": "2026-11-19T10:00:00.123000+00:00",
    "updated_at": "2026-11-19T10:00:05.456000+00:00"
  }
}
```

Times are UTC, in ISO 8601 with a `+00:00` offset. The reply is Markdown: short paragraphs, `**bold**` questions and labels, `-` bullets and `1.` steps. When `SHOW_TOKEN_USAGE=true` the response also has `usage` (the reply's token counts and estimated cost); it is off by default.

### `POST /agentic/stream`

The reply is reviewed in full before any text is sent, then streamed in small parts as `text/event-stream`. Each event is one `data:` line of JSON.

```text
data: {"sessionId": "6f0c...", "session_id": "6f0c...", "session": {...}}

data: {"text": "A week like that leaves "}

data: {"text": "very little room to breathe..."}

data: {"done": true, "sessionId": "6f0c...", "session_id": "6f0c...", "session": {...}}
```

1. **First event:** the chat's ID, before any text. For a new chat the title is a placeholder.
2. **`text` events:** the reply, in order. Join them to get the full text.
3. **`done` event:** the chat with its AI-written title (and `usage` when `SHOW_TOKEN_USAGE=true`).

**Error event.** When no reply can be written, for example during a Gemini rate limit or outage, there are no `text` events. One error event is sent instead and the stream ends:

```text
data: {"detail": "The AI is getting too many requests right now. Please try again in about 39 seconds.", "status": 429, "retry_after": 39, "session_discarded": true}
```

| Field | Meaning |
| :--- | :--- |
| `status` | `429` rate limit, `503` AI unavailable, `504` timed out |
| `detail` | A message that can be shown to the user |
| `retry_after` | Seconds to wait, on a rate limit |
| `session_discarded` | `true` when the new chat was not kept; retry without `session_id` |

The app should treat any event with `status` as an error: show `detail`, offer Retry, and keep the user's text. A message that may show risk (thoughts of dying, self-harm, an overdose) never gets this event: when the AI is unavailable it gets a fixed crisis reply with 988 and 911 instead.

## Chats

### `GET /agentic/sessions`

The user's chats, most recently updated first (an admin gets everyone's). Returns a list of chat objects, as in `session` above, plus `message_count` and `last_message_at` (the newest message's time).

### `GET /agentic/sessions/{id}/messages`

```json
{
  "session": { "id": "6f0c...", "title": "Stressful week at work", "...": "..." },
  "messages": [
    { "id": "...", "session_id": "6f0c...", "role": "user", "content": "I've had a really stressful week at work.", "metadata": {}, "created_at": "..." },
    { "id": "...", "session_id": "6f0c...", "role": "assistant", "content": "A week like that...", "metadata": {}, "created_at": "..." }
  ]
}
```

Messages are oldest first. A message from a grief note has `"isNote": true` in its `metadata`. A reply's `metadata.usage` holds its token use, shown only when `SHOW_TOKEN_USAGE=true`. A chat that is not the user's returns `404`.

### `DELETE /agentic/sessions/{id}`

Deletes the chat; the database cascade deletes its messages. Returns `{"success": true}`. A chat that is not the user's returns `404`. If the database user has no delete right, the answer is `403`.

## Health

| Endpoint | Response |
| :--- | :--- |
| `GET /` | `{"status": "ok", "app_name": "Held AI Agent API"}` |
| `GET /agentic/health` | `{"status": "ok", "app_name": "...", "database": {"connected": true}}`. Always `200`; when the database does not answer within 5 seconds, `status` is `degraded` and `connected` is `false` |

## Errors

| Code | When |
| :--- | :--- |
| `401` | Token missing, invalid or expired, or the user is not in `User` (see [Authentication](#authentication)) |
| `403` | Deleting a chat when the database user may not delete |
| `404` | The chat does not exist or is not the user's |
| `422` | Invalid body: empty or over 10,000 characters, or an unknown field. The sent values are not echoed |
| `429` | Gemini rate limit; the `Retry-After` header gives the wait (on the stream: the error event) |
| `503` | The AI or the database is unavailable, or the token could not be checked |
| `504` | The reply took longer than the time limit (240 seconds) |

## Grief notes in chat

The writer agent can read the user's own grief notes during a chat, with three read-only tools: `recent_notes`, `note_by_date` and `mood_summary`. They are bound to the token's user and take no user ID, so no one can read another user's notes. The app does not call them; the agent decides when a question needs the notes, for example "how has my last month been?".

## Memory

- **Within a chat:** the latest 20 messages, plus a rolling summary of older ones, updated in the background every 10 messages.
- **New chat:** starts fresh. Earlier chats are searched only when the user refers back to one ("last time I told you about my manager").
- **Everything comes from the token's user.** One user's chats and memory never reach another user.

More detail: [context.md](./context.md). Tables: [scheme.md](./scheme.md).

## Server settings

| Setting | Use |
| :--- | :--- |
| `DATABASE_URL` | The Held PostgreSQL database |
| `GEMINI_API_KEY` | Gemini key; use a paid key in production |
| `JWT_SECRET` | The key Held signs access tokens with |
| `HELD_API_BASE_URL` | Held API, to confirm a token signed with another key and as the profile fallback. Can be empty when `JWT_SECRET` is set |
| `SUPPORT_COUNTRY` | Country of the crisis lines replies give. Default `US` |
| `SHOW_TOKEN_USAGE` | `true` shows token use to the app. Default `false` |
| `SHOW_API_DOCS` | `true` serves Swagger at `/docs`. Default `false` |
