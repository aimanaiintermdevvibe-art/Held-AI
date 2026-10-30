# Held AI Agent API

A backend for signed-in Held users that accepts a prompt, remembers the user's
conversations, and returns a response through two endpoints: `POST /agentic/run` and `POST /agentic/stream`.

The agents provide mental-health education, caregiving and grief support:

1. **Intent & Safety Guardrail** classifies the prompt and checks for immediate risk.
2. **Clinical & Grief Reasoning Engine** analyzes requests that need specialist reasoning.
3. **Empathy & Persona Synthesizer** writes the response.
4. **Safety & Compliance Auditor** reviews the response before it is returned.

A supervisor selects the route. Simple support can skip clinical reasoning;
crisis requests take priority. Responses provide support and education, not diagnoses.

Users sign in with their Held account; conversations are stored in PostgreSQL and
remembered across turns and chats. Users can upload documents, and replies draw on the
relevant passages (RAG, below). There is no website or grief notes/calendar.

## Uploaded documents (RAG)

Documents are stored in Supabase with pgvector (migration
[002_documents_pgvector.sql](agent/migrations/002_documents_pgvector.sql), applied with
`python agent/migrations/apply.py 002_documents_pgvector.sql`):

1. **Upload** `POST /agentic/documents` (multipart field `file`; `.txt`, `.md`, `.pdf`, `.docx`,
   any length, up to 50 MB). Text is extracted (PDFs keep page numbers) and split into ~800-character
   chunks with 150 characters of overlap, which are stored straight away; the response is `202`
   with `"status": "queued"`. A background worker then embeds the chunks with `gemini-embedding-001`
   (768 dimensions), 100 per request, saving each batch (`processing` → `ready`; `paused` while
   Gemini's daily quota is used up, continuing by itself when it resets; `failed` on other errors).
   Follow it with `GET /agentic/documents/{id}` (`status`, `chunk_count` of `total_chunks`,
   `progress` %) or in the server console and `logs/document_ingestion.log`. Rate limits are waited
   out; a document interrupted by a restart resumes when the server starts, and a `failed` one
   resumes when the same file is uploaded again. The same file uploaded again otherwise returns the
   stored document (`200`, `"created": false`). Apply migration
   [004_document_ingestion_progress.sql](agent/migrations/004_document_ingestion_progress.sql) for this.
   On Gemini's free tier embedding runs at about **100 chunks a minute and 1,000 a day** (a
   7,000-chunk document takes about a week; a paid key removes this); search and chat use the
   chunks embedded so far.
   `agent/scripts/split_document.py <file>` splits a long PDF into page ranges, or a text/Word file
   into parts, if you prefer to upload sections separately.
2. **Retrieve** `POST /agentic/documents/search` `{"query": "...", "top_k": 5}` returns the
   nearest passages by cosine similarity.
3. **Chat** For a user with documents, each message is embedded and passages scoring at least
   `RAG_MIN_SIMILARITY` (default 0.55) are given to the reasoner, writer and safety auditor.
   A message about "my document/file/pdf" gets the nearest passages even below it. The reply
   cites the file, and `/agentic/run` (and the stream's `done` event) lists the `sources`.

Documents are private to the user who uploaded them; `GET /agentic/documents`,
`GET /agentic/documents/{id}` (with chunks) and `DELETE /agentic/documents/{id}` manage them.
`agent/scripts/rag_smoke_test.py` runs the whole flow against a running server and deletes
what it created; see `logs/rag_pgvector_setup_and_test.log`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `EMBEDDING_MODEL` | `gemini-embedding-001` | Must produce 768-dimensional vectors |
| `RAG_TOP_K` | `5` | Passages considered per chat message |
| `RAG_MIN_SIMILARITY` | `0.55` | Minimum cosine similarity for a passage to be used in chat |
| `DOCUMENT_MAX_BYTES` / `DOCUMENT_MAX_CHUNKS` | 50 MB / `0` (no limit) | Upload limits |
| `EMBEDDING_BATCH_SIZE` | `100` | Chunks per embedding request (Gemini's maximum) |
| `DOCUMENT_CHUNK_CHARS` / `DOCUMENT_CHUNK_OVERLAP` | `800` / `150` | Chunk size and overlap |

### Testing in Postman

Import `postman/Held_AI_Agent.postman_collection.json`. It logs in to Held automatically
(collection variables `email` / `password`, user 1 by default) and keeps the token fresh, then
runs: register, upload (`samples/willow-house-visitor-guide.docx`, saves `documentId`), list,
get with chunks, search, RAG chat (saves `sessionId`), follow-up, disease catalogue, delete.
The collection holds the test login and is git-ignored.

## Grief notes

The Held app's grief journal: one note per user per date, with a mood. Held's API stays the source
of truth; the agent writes and reads through it with the caller's own token and stores every note it
sees in `public.grief_notes` (migration [005_grief_notes.sql](agent/migrations/005_grief_notes.sql)),
with the guided workbook exercise the note answers (kept by the agent only). Moods are seeded into
`public."Mood"` by `python agent/scripts/seed_moods.py`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/agentic/grief-notes/exercises` | Guided workbook exercises (key, title, prompt) |
| `GET` | `/agentic/grief-notes/moods` | Moods a note can carry |
| `POST` | `/agentic/grief-notes/sync` | Store all of the user's existing Held notes |
| `GET` | `/agentic/grief-notes?page&limit&from&to` | Notes, newest first (synced from Held; stored copy if Held is down) |
| `POST` | `/agentic/grief-notes` | `{"date", "content", "mood" or "moodId", "exercise"?}`; one per date (409) |
| `GET` | `/agentic/grief-notes/{date}` | One note |
| `PATCH` | `/agentic/grief-notes/{date}` | `content` / `mood` (saved to Held) and/or `exercise` (null clears it) |

There is no delete: Held's API has none. In chat, a message that mentions the user's notes, journal,
diary or workbook gets their 10 most recent notes as context (`grief_notes`) for the writer and the
safety review; other messages do not. Held is the source of truth, so saving from chat is not offered:
Held needs a mood and allows one note per day, which the agent would have to guess.

## Disease catalogue

`public."Disease"` (migration [003_diseases.sql](agent/migrations/003_diseases.sql)) holds the
21 conditions from Held's public `GET /diseases`, with Held's ids. Fill or refresh it with
`python agent/scripts/seed_diseases.py` (safe to re-run; it updates changed entries and deletes
nothing). `GET /agentic/diseases` lists it. A user's own conditions still come from their Held profile.

## User modes

Held onboarding asks two questions, which give four modes:

| | Diagnosed | No diagnosis |
| --- | --- | --- |
| **Myself** | `myself_diagnosed` | `myself_undiagnosed` |
| **Caregiver** | `caregiver_diagnosed` | `caregiver_undiagnosed` |

For a caregiver, onboarding also records who they care for (`relation`, or
`relationOther` for "someone else"), and the selected conditions (up to 4, from Held's
`GET /diseases`) belong to that person. No selected conditions means no diagnosis.

On every turn the agent fetches the caller's profile from Held `GET /auth/me` with the
caller's own bearer token, derives the mode ([profile.py](agent/backend/ai_wrapper/profile.py)),
and passes it as `user_context` data to the clinical reasoner, the response writer and the
compliance auditor. Routing and off-topic answers do not use it. A user who has not
onboarded, or whose profile cannot be fetched, gets replies that assume neither mode.

## Project structure

```text
agent/
  backend/
    main.py                 # FastAPI application
    api/routers/agentic.py   # Single POST endpoint
    ai_wrapper/             # Gemini client, agents and supervisor
    core/config.py          # Environment settings
    schemas/agentic.py       # Prompt and response validation
  tests/                    # API and agent-flow tests
  .env.example              # Provider configuration template
  requirements.txt          # Runtime dependencies
  requirements-dev.txt      # Test dependencies
```

## Run

Requires Python 3.12+.

```powershell
cd agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Set GEMINI_API_KEY in .env before sending prompts. JWT_SECRET is only needed
# for protected session, user, and admin endpoints.
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Use a shared Gemini key, or configure all four per-agent keys. The supervisor uses
Agent 1's configuration. Model defaults are retained from the original project;
set the model variables to models available to your provider account.

Configuration is loaded from `agent/.env` or environment variables:

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Shared provider key |
| `GEMINI_AGENT1_API_KEY` through `GEMINI_AGENT4_API_KEY` | Optional per-agent key overrides |
| `GEMINI_AGENT1_MODEL` through `GEMINI_AGENT4_MODEL` | Model used by each agent |
| `GEMINI_AGENT1_THINKING_LEVEL` through `GEMINI_AGENT4_THINKING_LEVEL` | Per-agent thinking settings |
| `GEMINI_TIMEOUT_SECONDS` | Timeout per provider attempt; default and maximum: 45 seconds |
| `AGENTIC_RUN_TIMEOUT_SECONDS` | Total request deadline; default: 240 seconds |
| `AGENTIC_STREAM_CHUNK_SIZE` | Target character size per stream chunk; default: 24 |
| `AGENTIC_STREAM_DELAY_SECONDS` | Release delay between SSE chunks; default: 0.03 seconds |
| `DATABASE_URL` | Held PostgreSQL connection string |
| `JWT_SECRET` | Server-side HS256 key for agent API tokens; required by the local development terminal client |
| `HELD_API_BASE_URL` | Held API base URL (profiles for the server; sign-in, sign-up and onboarding for the terminal client) |
| `HELD_ACTIVE_USER` | Terminal client account: `1` uses `users/user1.json`, `2` uses `users/user2.json` |

### Authentication and user registration

A bearer token signed with `JWT_SECRET` is verified locally. A token signed with a key
the agent does not have (for example, the test Held API's) is accepted only when Held
confirms it through `GET /auth/me`. An expired token gets `401 TOKEN_EXPIRED` either way,
so clients refresh, and if Held cannot be reached the agent answers `503`.

A Held user missing from the agent's `public."User"` table (one who registered through
another Held environment, or just signed up) is added from their Held profile the first
time they authenticate. `POST /agentic/users/register` does this explicitly, and the
terminal client calls it right after sign-up, onboarding and sign-in. Held's `SELF` role
is stored as `USER` and `PARTNER` as `SPOUSE`; no password is stored, a Held profile
can never create an `ADMIN`, and existing rows are never changed.

### Terminal client and token refresh

The terminal client does no login or registration. Each test account has its own file:

| `HELD_ACTIVE_USER` | File | Account |
| --- | --- | --- |
| `1` | `users/user1.json` | hashirflutter.devvibe@gmail.com |
| `2` | `users/user2.json` | princejack66.pj@gmail.com |

A file holds the account's email and password, the Held API URL, its current token pair
and a copy of its last Held profile (see `users/user.example.json`). To switch user, set
`HELD_ACTIVE_USER` in `.env` and restart `python chat.py`.

On start the client signs the selected user in automatically: it uses the saved access
token, refreshes it when it is about to expire (or is rejected), and logs in with the
file's email and password only if the refresh token no longer works. Saved tokens that
belong to a different account are discarded. It then calls `POST /agentic/users/register`,
which adds the user to Supabase if missing. The mode comes from the user's Held profile.
Commands: `new` starts a new chat, `profile` shows your mode.

Held rotates the refresh token on every refresh and rejects a used one, so one token
workflow ([held_client.py](agent/backend/core/held_client.py)) writes every rotated pair
back to that user's file ([user_accounts.py](agent/backend/core/user_accounts.py)), and
refreshes are serialised across processes with a lock file beside it. A second terminal
or the next run continues from the latest pair instead of replaying an old one. The
account files hold passwords and live tokens and are git-ignored. The server needs no
Held credentials: it reads each caller's profile with the caller's own token.

## Request and response

```http
POST /agentic/run
Content-Type: application/json

{"prompt":"I feel overwhelmed and need help taking one small step."}
```

Successful response (illustrative):

```json
{
  "response": "That sounds difficult. Let's focus on one manageable step...",
  "usage": {
    "provider": "Google Gemini Developer API",
    "request_count": 4,
    "input_tokens": 1200,
    "output_tokens": 280,
    "thinking_tokens": 24,
    "total_tokens": 1504,
    "duration_ms": 4380.2,
    "estimated_cost_usd": 0.00106,
    "calls": []
  }
}
```

Usage includes provider-reported token totals and per-model-call details. Cost is
a standard paid-tier estimate for models with configured pricing; the API key's
free tier or account-specific billing adjustments are not visible to this app.
Unknown models have a `null` cost estimate.

With the server running, send a request from another PowerShell terminal:

```powershell
$body = @{ prompt = "I feel overwhelmed and need help taking one small step." } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/agentic/run" -ContentType "application/json" -Body $body
```

Every endpoint, including `/agentic/run` and `/agentic/stream`, requires the Held
application's bearer token; requests without one get `401` and no answer.
The token `sub` is the Held `User.id` (added to the agent's database on first use);
session ownership comes from that claim, never from a request body. `SELF` and
`CAREGIVER` tokens are scoped to their own sessions; `ADMIN` can access all
sessions and the read-only user endpoints. `JWT_SECRET` remains server-only and
must never be sent as a bearer token.

`prompt` is required and accepts 1 to 10,000 characters. A new session is created
automatically when `session_id` is omitted; user and assistant turns are saved in
the existing `ChatSession` and `ChatMessage` tables. The agent makes no schema
changes; besides chats, it only adds rows for Held users it has not seen before.
Invalid input returns 422, unavailable
generation returns 503, and requests exceeding the total time budget return 504.
Unsafe or rejected drafts
are replaced with a safe fallback. Crisis routing preserves an immediate safety
fallback if the provider fails.

Error responses use `{"detail":"..."}`; validation errors return a structured
`detail` list. Additional request fields are rejected.

`GET /` (the process is up) and `GET /agentic/health` (plus whether the database
answers) are public and need no token. Session history and session deletion are
available to authenticated callers. User listing/profile and database overview
require an `ADMIN` token. The current Held schema has no Disease table, so the disease
endpoint is unavailable.

## Streaming API

`POST /agentic/stream` accepts the same request body and streams Server-Sent Events
(`text/event-stream`). The pipeline completes its safety and compliance checks
before any answer text is sent; approved text is then delivered in `chunk` events,
followed by `done`. This keeps unreviewed drafts out of the client while providing
an incremental response transport.

```powershell
$body = '{"prompt":"I feel overwhelmed and need help taking one small step."}'
curl.exe -N "http://127.0.0.1:8000/agentic/stream" -H "Content-Type: application/json" -d $body
```

Events have JSON data, for example `event: chunk` with `{"text":"..."}`, then
`event: done` with `{"done":true}`. A timeout or provider failure is sent as an
`event: error` with the same sanitized detail used by `/agentic/run`.

## Validate

```powershell
cd agent
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests exercise the actual API and agent flow with a fake provider. They do not
verify live Gemini credentials, model availability or response quality.

```
Project3-Held-AI---Chatbot-main
├─ agent
│  ├─ backend
│  │  ├─ ai_wrapper
│  │  │  ├─ agents
│  │  │  │  ├─ agent1_intent_guardrail
│  │  │  │  │  ├─ agent.py
│  │  │  │  │  ├─ policy.py
│  │  │  │  │  ├─ prompt.py
│  │  │  │  │  └─ __init__.py
│  │  │  │  ├─ agent2_clinical_reasoner
│  │  │  │  │  ├─ agent.py
│  │  │  │  │  ├─ prompt.py
│  │  │  │  │  └─ __init__.py
│  │  │  │  ├─ agent3_empathy_synthesizer
│  │  │  │  │  ├─ agent.py
│  │  │  │  │  ├─ openings.py
│  │  │  │  │  ├─ prompt.py
│  │  │  │  │  └─ __init__.py
│  │  │  │  ├─ agent4_compliance_auditor
│  │  │  │  │  ├─ agent.py
│  │  │  │  │  ├─ prompt.py
│  │  │  │  │  └─ __init__.py
│  │  │  │  ├─ common
│  │  │  │  │  ├─ base.py
│  │  │  │  │  ├─ serialization.py
│  │  │  │  │  └─ __init__.py
│  │  │  │  └─ __init__.py
│  │  │  ├─ client.py
│  │  │  ├─ contracts.py
│  │  │  ├─ memory.py
│  │  │  ├─ orchestrator.py
│  │  │  ├─ supervisor
│  │  │  │  ├─ agent.py
│  │  │  │  ├─ prompt.py
│  │  │  │  ├─ schemas.py
│  │  │  │  └─ __init__.py
│  │  │  ├─ usage.py
│  │  │  └─ __init__.py
│  │  ├─ api
│  │  │  ├─ routers
│  │  │  │  ├─ agentic.py
│  │  │  │  ├─ sessions.py
│  │  │  │  └─ __init__.py
│  │  │  └─ __init__.py
│  │  ├─ core
│  │  │  ├─ auth.py
│  │  │  ├─ config.py
│  │  │  └─ __init__.py
│  │  ├─ main.py
│  │  ├─ schemas
│  │  │  ├─ agentic.py
│  │  │  ├─ sessions.py
│  │  │  └─ __init__.py
│  │  └─ __init__.py
│  ├─ migrations
│  │  └─ 001_user_and_chat_tables.sql
│  ├─ pyproject.toml
│  ├─ pytest-cache-files-xo9t3z0k
│  │  └─ README.md
│  ├─ README.md
│  ├─ requirements-dev.txt
│  ├─ requirements.txt
│  └─ tests
│     ├─ conftest.py
│     ├─ test_agentic.py
│     ├─ test_auth.py
│     ├─ test_chat_refresh.py
│     ├─ test_memory.py
│     ├─ test_openings.py
│     ├─ test_policy.py
│     └─ test_sessions.py
├─ document
│  ├─ api.md
│  ├─ context.md
│  └─ scheme.md
├─ main.py
├─ pyrightconfig.json
├─ README.md
└─ requirements.txt

```