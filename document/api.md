# Held AI Agent API

The Held AI Agent API provides multi-agent AI support and persists conversations in Held's shared PostgreSQL database.

---

## Base URLs

- **Local Agent Server**: `http://127.0.0.1:8000`
- **Production (Render)**: `https://held-ai-chatbot.onrender.com`

---

## Summary of Endpoints

| Category | Method | Endpoint | Description |
| :--- | :--- | :--- | :--- |
| **Agent** | `POST` | `/agentic/stream` | Progressive Server-Sent Events (SSE) stream (with automatic session creation & details) |
| **Agent** | `POST` | `/agentic/run` | Standard JSON response payload (with automatic session creation & details) |
| **Health** | `GET` | `/` | Public liveness check; touches no database or Held API |
| **Agent** | `GET` | `/agentic/health` | Public service & Held PostgreSQL health status |
| **Sessions** | `GET` | `/agentic/sessions` | List the authenticated user's chat sessions (admins can list all) |
| **Sessions** | `GET` | `/agentic/sessions/{id}/messages` | Retrieve chronological message history |
| **Sessions** | `DELETE` | `/agentic/sessions/{id}` | Delete a chat session and all its messages |
| **Users** | `GET` | `/agentic/users` | List registered users (read-only view) |
| **Users** | `GET` | `/agentic/users/{id}` | Get user profile by ID (read-only view) |
| **Users** | `POST` | `/agentic/users/register` | Add the signed-in Held user to the agent's database if missing; returns their row |
| **Documents** | `POST` | `/agentic/documents` | Upload a `.txt`, `.md`, `.pdf` or `.docx` of any length (multipart field `file`, up to 50 MB); returns `202` and embeds the chunks in the background |
| **Documents** | `GET` | `/agentic/documents` | List your documents |
| **Documents** | `GET` | `/agentic/documents/{id}` | Status (`queued`/`processing`/`ready`/`failed`), progress, and a page of chunks (`chunk_offset`, `chunk_limit`) |
| **Documents** | `DELETE` | `/agentic/documents/{id}` | Delete a document and its chunks |
| **Documents** | `POST` | `/agentic/documents/search` | `{"query", "top_k", "document_ids"?}`: nearest passages by cosine similarity |
| **Diseases** | `GET` | `/agentic/diseases` | The disease catalogue (`public."Disease"`, seeded from Held `GET /diseases` by `agent/scripts/seed_diseases.py`) |
| **Grief notes** | `GET` | `/agentic/grief-notes/exercises` | Guided workbook exercises |
| **Grief notes** | `GET` | `/agentic/grief-notes/moods` | Mood catalogue (seeded from Held) |
| **Grief notes** | `POST` | `/agentic/grief-notes/sync` | Store all of the user's Held notes |
| **Grief notes** | `GET` | `/agentic/grief-notes` | List notes (`page`, `limit`, `from`, `to`) |
| **Grief notes** | `POST` | `/agentic/grief-notes` | Write a note for a date (via Held) |
| **Grief notes** | `GET` | `/agentic/grief-notes/{date}` | One note |
| **Grief notes** | `PATCH` | `/agentic/grief-notes/{date}` | Edit content/mood (Held) or exercise (agent)
| **Admin** | `GET` | `/agentic/admin/overview` | Database summary entity counts |

> **Configuration Note**: `DATABASE_URL` and `JWT_SECRET` are read from the local `.env` or the deployment environment. The agent writes to `chat_sessions`, `chat_messages`, `documents` and `document_chunks` (pgvector, migration 002), and adds `User` rows for Held users it has not seen before; it does not create tables (see `agent/migrations/001_user_and_chat_tables.sql`). If the Supabase direct host is unreachable (it is IPv6-only), set `DATABASE_POOLER_URL` to the IPv4 session pooler URL. `MEMORY_CONTEXT_CHAR_BUDGET` (default `120000`) sets how much history is sent verbatim to the agents. Summarisation fires once **10 messages** (user + assistant combined) have built up after the last summary, and runs in a background queue — the user is never delayed. The configured LAN database must be reachable from the machine running the agent.

## Authentication

`GET /` and `GET /agentic/health` are public. Session, user, and admin endpoints require the Held application's signed HS256 access token:

```http
Authorization: Bearer <held_access_token>
```

The token's `sub` claim must be the `id` of a row in `public."User"` (see `agent/migrations/001_user_and_chat_tables.sql`); a Held user missing from that table is added from their Held profile (`GET /auth/me`) on first use, and only a token for an id Held does not know returns `401` with code `USER_NOT_FOUND`. Tokens signed with `JWT_SECRET` are verified locally; a token signed with another key is accepted only when Held confirms it through `GET /auth/me`. The user's stored `role` is normalized to `ADMIN`, `CAREGIVER`, or `SELF` (a missing role is `SELF`). `CAREGIVER` and `SELF` can access only sessions owned by their token subject; `ADMIN` can access all sessions. A `user_id` supplied in a request body is ignored for ownership. User listing/profile and database overview endpoints are admin-only.

`POST /agentic/run` and `POST /agentic/stream` **require** a valid token whose subject exists in `public."User"`; every turn is saved and remembered through [Context memory](#7-context-memory). A missing token returns `401 TOKEN_MISSING`, an invalid or expired one `401 TOKEN_INVALID` / `TOKEN_EXPIRED`, and a token for an id not in `User` returns `401 USER_NOT_FOUND`. The same token protects the session, user, and admin routes. `JWT_SECRET` is a server-only verification key; never send it in a request or use it as the bearer token.

For each generation request, the agent also fetches the caller's profile from the Held API (`GET /auth/me`) using the caller's own bearer token, so the server needs no Held credentials. The onboarding answers in it select one of four user modes (`myself_diagnosed`, `myself_undiagnosed`, `caregiver_diagnosed`, `caregiver_undiagnosed`), which are passed to clinical reasoning, response synthesis and the compliance review as `user_context`. If Held profile lookup fails, the agent logs a warning and continues without a user mode.

---

## 1. `POST /agentic/stream` - Progressive SSE Stream

Executes clinical intent classification, supervised planning, and clinical safety auditing, delivering the response in real-time sequential chunks via Server-Sent Events (`text/event-stream`).

### Automatic Session Management
This endpoint replaces standalone `POST /agentic/sessions` and `GET /agentic/sessions/{id}`:
- **No `session_id` provided (New Session)**: A new session is created in `public.chat_sessions`, and both turns are appended to `public.chat_messages`.
- **Existing `session_id` provided**: The session must belong to the token subject (unless the caller is an admin). Missing or inaccessible sessions return `404`.
- **Pure Conversational Text for User**: Streamed text chunks (`{"text": "..."}`) contain **pure conversational response only**. Session IDs, debug indicators, and metadata are never mixed into the message text.
- **Programmatic Session State**: The active `sessionId`, `session_id`, and complete `session` object are delivered in structured SSE JSON keys on the first event and final completion event for client state tracking.

### Request Body

```json
{
  "prompt": "I feel overwhelmed and need help taking one small step.",
  "session_id": "22006c3e-dc6f-4862-9503-5d408ddc6c97",
  "title": "Overwhelm Reflection & Steps",
  "metadata": {"source": "mobile_app"}
}
```

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `prompt` | `string` | **Yes** | 1 to 10,000 characters. |
| `session_id` | `string` | Optional | Existing session ID to append to. **If omitted, a new session is automatically created.** |
| `user_id` | `string` | Optional | Ignored; ownership is taken from the signed token subject. |
| `title` | `string` | Optional | Ignored; the AI names each new session itself. |
| `metadata` | `object` | Optional | Custom JSON metadata dictionary to store on the session (the `memory` key is reserved). |
| `session` | `object` | Optional | Alternative nested session object containing `id`, `user_id`, `title`, `metadata`. |

### How to Use

#### cURL
```bash
# New session (session_id omitted - auto-created in database)
curl -N -X POST "https://held-ai-chatbot.onrender.com/agentic/stream" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"prompt": "I feel overwhelmed and need help taking one small step."}'
```
> **Note:** The `-N` flag disables cURL's internal buffering to display chunks immediately.

#### JavaScript (`fetch` / Browser)
```javascript
let currentSessionId = null; // Store across turns

async function chat(prompt) {
  const payload = { prompt };
  if (currentSessionId) {
    payload.session_id = currentSessionId;
  }

  const response = await fetch("https://held-ai-chatbot.onrender.com/agentic/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Accept": "text/event-stream",
    },
    body: JSON.stringify(payload),
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;

      const data = JSON.parse(trimmed.slice(5).trim());

      // Capture session ID automatically for next turns
      if (data.sessionId && !currentSessionId) {
        currentSessionId = data.sessionId;
      }

      // Display pure conversational text to the user
      if (data.text) {
        document.getElementById("chat-box").innerText += data.text;
      }

      // Stream completed
      if (data.done) {
        console.log("Chat turn complete. Session ID:", currentSessionId);
      }
    }
  }
}
```

#### Stream Event Payloads

```text
data: {"sessionId": "session-text-id", "session_id": "session-text-id", "session": {"id": "session-text-id", "user_id": "held-user-id-from-token", "title": "I feel overwhelmed...", "status": "ACTIVE", "metadata": null, "created_at": "...", "updated_at": "..."}}

data: {"text": "It is completely understandable "}

data: {"text": "to feel that way. Let us focus "}

data: {"text": "on just one small, manageable step right now."}

data: {"done": true, "sessionId": "22006c3e-dc6f-4862-9503-5d408ddc6c97", "session_id": "22006c3e-dc6f-4862-9503-5d408ddc6c97", "session": {"id": "22006c3e-dc6f-4862-9503-5d408ddc6c97", ...}}
```

---

## 2. `POST /agentic/run` - Standard JSON

Executes the pipeline and returns the complete, audited response along with the active `session_id` and complete `session` entity.

### Automatic Session Management
- **No `session_id` provided**: Automatically creates a session in `chat_sessions`, saves both turns in `chat_messages`, and returns the active `session_id`.
- **`session_id` provided**: Appends user message and assistant reply to the existing session thread.
- **Pure Conversational Text**: The `response` field contains purely the clean agent message.

### Request Body
```json
{
  "prompt": "Can you give me 3 brief tips for caregiver burnout?",
  "session_id": "22006c3e-dc6f-4862-9503-5d408ddc6c97",
  "title": "Caregiver Burnout Tips",
  "metadata": {"source": "mobile_app"}
}
```

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `prompt` | `string` | **Yes** | 1 to 10,000 characters. |
| `session_id` | `string` | Optional | Existing session ID to append to. **If omitted, a new session is automatically created.** |
| `user_id` | `string` | Optional | Ignored; ownership is taken from the signed token subject. |
| `title` | `string` | Optional | Ignored; the AI names each new session itself. |
| `metadata` | `object` | Optional | Custom JSON metadata dictionary to store on the session (the `memory` key is reserved). |
| `session` | `object` | Optional | Alternative nested session object containing `id`, `user_id`, `title`, `metadata`. |

### Python Example
```python
import requests

response = requests.post(
    "https://held-ai-chatbot.onrender.com/agentic/run",
    headers={"Authorization": f"Bearer {held_access_token}"},
    json={"prompt": "Can you give me 3 brief tips for caregiver burnout?"}
)
data = response.json()

# Pure assistant response text:
print(data["response"])

# Programmatic session tracking:
print("Session ID:", data["session_id"])
print("Session Details:", data["session"])
```

### Response (`200 OK`)
```json
{
  "response": "1. Schedule micro-breaks throughout the day...\n2. Set clear emotional boundaries...\n3. Connect with caregiver support groups...",
  "session_id": "22006c3e-dc6f-4862-9503-5d408ddc6c97",
  "session": {
    "id": "session-text-id",
    "user_id": "held-user-id-from-token",
    "title": "Caregiver Burnout Tips",
    "status": "active",
    "metadata": {
      "source": "mobile_app"
    },
    "session_summary": null,
    "last_summarized_message_id": null,
    "total_message_count": 2,
    "created_at": "2026-10-01T12:00:00Z",
    "updated_at": "2026-10-01T12:05:00Z"
  }
}
```

> **`session_summary`** is `null` until the 10-message threshold is first crossed, after which it holds a compact rolling summary of the conversation produced in the background.

---

## 3. Chat Session Management Endpoints

> **Integrated Session Lifecycle**: Standalone `POST /agentic/sessions` (create session) and `GET /agentic/sessions/{id}` (get session details) have been deprecated and return `405 Method Not Allowed`. When chatting, send your request to `/agentic/stream` or `/agentic/run`; if `session_id` is omitted, a new session is created automatically.

### 3.1 List Sessions: `GET /agentic/sessions`
Returns the caller's sessions ordered by `updatedAt DESC`. Admins receive all sessions. There is no caller-selected user filter.

```bash
# Get all sessions
curl -X GET "https://held-ai-chatbot.onrender.com/agentic/sessions" -H "Authorization: Bearer $HELD_ACCESS_TOKEN"

# Filter by user ID
curl -X GET "https://held-ai-chatbot.onrender.com/agentic/sessions" -H "Authorization: Bearer $HELD_ADMIN_ACCESS_TOKEN"
```

#### Response (`200 OK`)
```json
[
  {
    "id": "22006c3e-dc6f-4862-9503-5d408ddc6c97",
    "user_id": "usr_fc24612762006896",
    "title": "I am dealing with a recent loss and need guidance",
    "status": "active",
    "metadata": {},
    "session_summary": "The user is grieving a recent loss and seeks emotional support and practical coping strategies.",
    "last_summarized_message_id": "8c1e2f0a-5b7d-4c9e-a1f3-2d6b9e0c4a71",
    "total_message_count": 14,
    "message_count": 14,
    "last_message_at": "2026-10-01T12:32:47.414000Z",
    "created_at": "2026-10-01T11:44:24.194000Z",
    "updated_at": "2026-10-01T12:32:47.414000Z"
  }
]
```

| Field | Type | Description |
| :--- | :--- | :--- |
| `session_summary` | `string \| null` | Compact rolling summary produced by the background summariser. `null` until the first 10-message threshold is crossed. |
| `total_message_count` | `int` | Total number of messages (user + assistant) saved in this session. |
| `last_summarized_message_id` | `uuid \| null` | Last message folded into `session_summary`. The next summary fires once 10 newer messages exist. |

### 3.2 Get Session Conversation History: `GET /agentic/sessions/{id}/messages`
Retrieves all turns (both user prompts and assistant replies) in chronological order.

```bash
curl -X GET "https://held-ai-chatbot.onrender.com/agentic/sessions/22006c3e-dc6f-4862-9503-5d408ddc6c97/messages" -H "Authorization: Bearer $HELD_ACCESS_TOKEN"
```

#### Response (`200 OK`)
```json
{
  "session": {
    "id": "22006c3e-dc6f-4862-9503-5d408ddc6c97",
    "title": "I am dealing with a recent loss and need guidance"
  },
  "messages": [
    {
      "id": "11111111-1111-1111-1111-111111111111",
      "session_id": "22006c3e-dc6f-4862-9503-5d408ddc6c97",
      "role": "user",
      "content": "I am dealing with a recent loss and need a moment to reflect.",
      "created_at": "2026-10-01T11:45:00Z"
    },
    {
      "id": "22222222-2222-2222-2222-222222222222",
      "session_id": "22006c3e-dc6f-4862-9503-5d408ddc6c97",
      "role": "assistant",
      "content": "I hear how heavy and tender your heart must be right now...",
      "created_at": "2026-10-01T11:45:05Z"
    }
  ]
}
```

### 3.3 Delete Session: `DELETE /agentic/sessions/{id}`
Deletes the conversation session. Thanks to database foreign key cascading (`ON DELETE CASCADE`), all associated messages are also deleted.

```bash
curl -X DELETE "https://held-ai-chatbot.onrender.com/agentic/sessions/1baa9da8-c663-4cb1-ad7f-3d37ef0b56b5" -H "Authorization: Bearer $HELD_ACCESS_TOKEN"
```

#### Response (`200 OK`)
```json
{
  "success": true
}
```

---

## 4. Health & Database Overview Endpoints

### 4.1 Liveness Check: `GET /`
Public. Confirms the API process is running; it does not check the database or the Held API.

```bash
curl -X GET "https://held-ai-chatbot.onrender.com/"
```

#### Response (`200 OK`)
```json
{
  "status": "ok",
  "app_name": "Held AI Agent API"
}
```

### 4.2 Health Check: `GET /agentic/health`
Public. Checks FastAPI service and Held PostgreSQL connection status. The database gets 5 seconds to answer. The response is always `200 OK`: when the database is unreachable, `status` is `degraded` and `connected` is `false`.

```bash
curl -X GET "https://held-ai-chatbot.onrender.com/agentic/health"
```

#### Response (`200 OK`)
```json
{
  "status": "ok",
  "app_name": "Held AI Agent API",
  "database": {
    "connected": true
  }
}
```

### 4.3 Database Entity Overview: `GET /agentic/admin/overview`
Returns read-only counts for `User`, `chat_sessions`, and `chat_messages`.

```bash
curl -X GET "https://held-ai-chatbot.onrender.com/agentic/admin/overview" -H "Authorization: Bearer $HELD_ADMIN_ACCESS_TOKEN"
```

---

## 5. User & Disease Endpoints (Read-Only)

> **Access Policy**: The Agent API has read-only access to `User` and `GriefNote`; it writes to neither table. The connected database does not contain a `Disease` table. User endpoints require an admin token.

### 5.1 List Users: `GET /agentic/users`
Retrieves registered user profiles from `public."User"`.

```bash
curl -X GET "https://held-ai-chatbot.onrender.com/agentic/users" -H "Authorization: Bearer $HELD_ADMIN_ACCESS_TOKEN"
```

#### Response (`200 OK`)
```json
[
  {
    "id": "usr_fc24612762006896",
    "email": "eleanor@example.com",
    "fullName": "Eleanor Vance",
    "username": "eleanor_vance",
    "role": "USER",
    "relation": "SELF",
    "createdAt": "2026-10-01T12:00:00Z"
  }
]
```

### 5.2 Get User Profile: `GET /agentic/users/{id}`
Retrieves a single user's profile details.

```bash
curl -X GET "https://held-ai-chatbot.onrender.com/agentic/users/usr_fc24612762006896" -H "Authorization: Bearer $HELD_ADMIN_ACCESS_TOKEN"
```

### 5.3 Diseases
Unavailable in this database: there is no `Disease` table, so this endpoint returns `404`.

```bash
```

---

## 6. Error Handling

Standard HTTP status codes are returned for error conditions:

| Code | Name | Description |
| :--- | :--- | :--- |
| `400` | Bad Request | Missing required parameters or invalid syntax. |
| `404` | Not Found | Session or resource ID does not exist in the database. |
| `405` | Method Not Allowed | Standalone session creation or details requested (use `/stream` or `/run` instead). |
| `422` | Unprocessable Entity | Validation failed (e.g. empty prompt or prompt exceeding 10,000 chars). |
| `503` | Service Unavailable | Downstream AI provider or database unreachable. |
| `504` | Gateway Timeout | Processing exceeded timeout limit. |

---

## 7. Context memory & Summarisation

`/agentic/run` and `/agentic/stream` remember earlier turns of a conversation; there are no separate conversation routes.

### How memory is loaded

- **Requires** a valid token for a user in `public."User"`; send the `session_id` returned by the first turn to continue a conversation. Ownership comes from the token; a `user_id` in the body is never used.
- **Before generation**: the server confirms the session belongs to the caller (admins excepted), loads its `chat_messages` rows in order, adds short recaps (summary + the user's last 6 messages) of the user's 10 most recent other conversations as `previous_conversations`, and passes them to the agents as context — not as instructions. New conversations also receive these recaps, so the agent remembers the user across chats.
  - If all messages fit within `MEMORY_CONTEXT_CHAR_BUDGET` (default 120 000 chars), the full verbatim history is sent.
  - If history exceeds the budget, the agents receive `chat_sessions.session_summary` plus every message after `last_summarized_message_id` (at most the latest 20 prompt/answer pairs).

### 10-message threshold summarisation

Summarisation is **anchor-based**: it counts messages stored after `last_summarized_message_id`, and runs entirely in the background.

| Step | What happens |
| :--- | :--- |
| Each turn saved | The user message and assistant reply are inserted into `chat_messages`; each insert increments `total_message_count` in the same statement. |
| After each turn | A summarisation job is placed on an in-process `asyncio.Queue`. The user response has already been sent — they are never blocked. |
| Consumer (idle) | A background worker re-reads the session and its messages and counts those after `last_summarized_message_id`. |
| ≥ 10 new messages | An LLM call merges them into the previous summary (≤ 250 words). The result is written to `chat_sessions.session_summary` and `last_summarized_message_id` is set to the newest summarised message. |
| Threshold not met | Job is discarded; no database write. |

```
Example timeline (messages = user + assistant counted together):

  msg #1–#2   (turn 1): 2 messages after anchor  < 10 → skip
  msg #3–#4   (turn 2): 4 messages after anchor  < 10 → skip
  ...
  msg #9–#10  (turn 5): 10 messages after anchor ≥ 10 → SUMMARISE, anchor = msg #10
  msg #11–#12 (turn 6): 2 messages after anchor  < 10 → skip
  ...
  msg #19–#20 (turn 10): 10 messages after anchor ≥ 10 → SUMMARISE, anchor = msg #20
```

### Where each value is stored

| Data | Column |
| :--- | :--- |
| Conversation owner | `chat_sessions.user_id` (→ `User.id`) |
| AI-generated title | `chat_sessions.title` |
| Rolling summary | `chat_sessions.session_summary` |
| Summary anchor | `chat_sessions.last_summarized_message_id` |
| Message count | `chat_sessions.total_message_count` |
| Client-supplied metadata | `chat_sessions.metadata` (nothing server-owned is stored here) |
| Each prompt / answer | `chat_messages.role`, `content`, `created_at` |

### Session title

The AI names every new session after its first answer (3–6 words; the prompt excerpt is used if naming fails). In `/stream` the first event carries a placeholder title and the final `done` event delivers the AI-generated title.
