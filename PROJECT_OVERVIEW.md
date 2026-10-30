# Held AI Chatbot — Project Overview

## Executive summary

This repository contains a Python/FastAPI backend for an AI mental-health,
caregiving, and grief-support chatbot. It uses Google Gemini through a
multi-agent response pipeline, authenticates users with Held-issued bearer
tokens, stores conversations in PostgreSQL, and can personalize replies with a
user profile fetched from the Held application API.

The project is a backend and terminal client, not a complete user-facing
application: there is no web or mobile UI in this repository. Despite clinical
language in some internal component names, the chatbot is designed for support
and education, **not to diagnose diseases or prescribe treatment**.

## What is implemented

### Conversation routing and response modes

The agent pipeline in
[`agent/backend/ai_wrapper/orchestrator.py`](./agent/backend/ai_wrapper/orchestrator.py)
classifies and routes each turn:

| Mode | Purpose |
| --- | --- |
| `general_support` | Supportive response to ordinary emotional-support requests. |
| `clinical_support` | Educational mental-health or caregiving support, after structured clinical reasoning. |
| `grief` | Companioning response to loss; intentionally avoids diagnosing or pathologizing grief. |
| `crisis` | Prioritizes immediate safety and uses a crisis-safe fallback if generation fails. |
| `off_topic` | Answers unrelated questions directly, without unnecessary mental-health redirection. |

The intent guardrail, supervisor, and response pipeline are implemented in
[`agent/backend/ai_wrapper/agents/`](./agent/backend/ai_wrapper/agents/) and
[`agent/backend/ai_wrapper/supervisor/`](./agent/backend/ai_wrapper/supervisor/).
Generated responses go through a compliance audit before being returned. The
streaming endpoint also completes this review before sending any answer text.

### “Myself” and caregiver profile personalization

There are two related but distinct kinds of context:

1. **Conversation mode** is selected from the current prompt (for example,
   grief or clinical support).
2. **User mode** comes from the user's Held onboarding answers and is one of
   four: `myself_diagnosed`, `myself_undiagnosed`, `caregiver_diagnosed`,
   `caregiver_undiagnosed`.

For each generation, the API requests the caller's profile from Held
`GET /auth/me` using the caller's own bearer token. These fields select the mode:

- `role` — `SELF` (chatting for themselves) or `CAREGIVER`. A user who has not
  onboarded has no role, and no mode is assumed.
- `relation` and `relationOther` — who a caregiver supports.
- `diseases[].name` — the diagnosed conditions (the cared-for person's, for a
  caregiver); an empty list means no diagnosis.

The mode is sent as `user_context` data to the clinical reasoner, the response
writer and the compliance auditor; it is not used for routing or off-topic
answers. It informs response wording and relevance; it does not change the
safety rules or make the chatbot a diagnostic tool, and in an undiagnosed mode
the prompts forbid suggesting a condition. The modes are derived in
[`agent/backend/ai_wrapper/profile.py`](./agent/backend/ai_wrapper/profile.py),
the per-mode guidance is in
[`agent/backend/ai_wrapper/agents/agent3_empathy_synthesizer/prompt.py`](./agent/backend/ai_wrapper/agents/agent3_empathy_synthesizer/prompt.py),
and the Held profile request is in
[`agent/backend/core/auth.py`](./agent/backend/core/auth.py). The mode itself is
set in the Held app's onboarding. The terminal client [`chat.py`](./chat.py) does not
log in or register interactively: it signs in the test account chosen by
`HELD_ACTIVE_USER` in `.env` (`users/user1.json` or `users/user2.json`) and keeps that
account's tokens refreshed, using
[`agent/backend/core/held_client.py`](./agent/backend/core/held_client.py).

Profile personalization is **best effort**: if the Held profile request fails,
the backend logs a warning and continues with an empty profile. A response can
therefore be generated without the user's role, relationship, or condition
context.

### Conversation memory

Authenticated turns are associated with a database session. When a session ID
is supplied, the backend checks ownership and loads that session's messages in
chronological order. It sends full history when it fits the configured
character budget; otherwise it uses the stored rolling summary and up to the
latest 40 messages (20 user/assistant pairs). Older context is summarized
asynchronously after 10 unsummarized messages. The implementation also loads
short recaps of up to 10 of the user's other recent conversations.

Prompts and final, audited answers are saved as chat messages. Memory is treated
as context rather than instructions. Each earlier conversation's recap also lists
`safety_concerns`: the user's messages there that the local crisis check flags
(for example thoughts of dying), even when they came before the recent messages.
The writer recalls earlier chats when asked, connects people and situations the
user mentioned before, and, when an earlier chat had a safety concern and the
user is now talking about how they feel, checks in gently once per chat. Routing
never sees other conversations, and off-topic answers do not use them. See
[`agent/backend/ai_wrapper/memory.py`](./agent/backend/ai_wrapper/memory.py)
and
[`agent/backend/api/routers/agentic.py`](./agent/backend/api/routers/agentic.py).

### API and client

- `POST /agentic/run` returns a complete JSON response.
- `POST /agentic/stream` streams the reviewed response as Server-Sent Events.
- Authenticated session routes list sessions, retrieve message history, and
  delete a session and its messages.
- Admin-only routes expose read-only user information and database counts.
- [`chat.py`](./chat.py) is a small streaming terminal client.
- [`main.py`](./main.py) exposes the FastAPI application from the repository
  root; the backend application is
  [`agent/backend/main.py`](./agent/backend/main.py).

Requests are authenticated with a Held HS256 bearer token, verified with
`JWT_SECRET` or, when signed with another key, confirmed by Held `GET /auth/me`.
The token subject must correspond to a row in `public."User"`; a Held user who is
missing is added from their Held profile on first use (also via
`POST /agentic/users/register`). The backend obtains the role from that user row. Non-admin users can access only their own sessions. Session and
profile ownership is derived from the validated token, not a client-provided
user ID.

## Database and external services

### PostgreSQL connection

The backend uses `asyncpg` to connect to the configured PostgreSQL database.
Configuration supports `DATABASE_URL` and an optional `DATABASE_POOLER_URL`
fallback. The migration
[`agent/migrations/001_user_and_chat_tables.sql`](./agent/migrations/001_user_and_chat_tables.sql)
documents/creates the user and conversation tables used by the agent.

| Database object | How the chatbot uses it |
| --- | --- |
| `public."User"` | Validates that a token subject exists and reads the user's role; adds Held users it has not seen before (never changes existing rows, stores no password). The admin user endpoints expose limited user fields. |
| `public.chat_sessions` | Stores the conversation owner, title, status, timestamps, and rolling-summary fields. |
| `public.chat_messages` | Stores user prompts and final assistant replies; messages are reloaded as conversation context. |

The agent writes conversation data to `chat_sessions` and `chat_messages`.
Profile registration data is fetched from the Held API rather than being read
from the PostgreSQL user row for response personalization.

### Held API profile integration

The backend makes an external request to the configured Held API for the
authenticated user's profile. Held API login/refresh credentials or an initial
token are configured server-side; the agent keeps the resulting token pair in
memory. The fetched profile is passed to the response writer as personalization
context. Profile failures are non-fatal and result in no profile-based
personalization for that request.

### Disease data and diagnosis

The disease catalogue is stored in `public."Disease"` (migration
[`003_diseases.sql`](./agent/migrations/003_diseases.sql)), seeded from Held's public
`GET /diseases` with [`seed_diseases.py`](./agent/scripts/seed_diseases.py) (21
conditions; ids are Held's, the same ones in a user's profile `diseases`; re-running
updates changed entries and deletes nothing). `GET /agentic/diseases` lists it, and the
admin overview counts it. **Each user's own conditions are not stored in Supabase:**
they still come live from the Held profile, whose `diseases` select the user's mode.

Retrieval (RAG) covers only documents users upload themselves: they are chunked,
embedded with `gemini-embedding-001` and stored in Supabase with pgvector
(`documents`, `document_chunks`; migration
[`002_documents_pgvector.sql`](./agent/migrations/002_documents_pgvector.sql)), and
relevant passages are given to the agents as `document_excerpts` (see
[`agent/backend/rag/`](./agent/backend/rag/)). There is no curated medical reference
source. The system can discuss concerns educationally and account for a
profile-provided diagnosis, but it does **not** establish or verify a diagnosis.

## What remains / recommended next work

### Needed to make profile-based care reliable

- Confirm and document the Held profile API's actual response schema, including
  how diagnoses/conditions are represented. The current prompt builder expects
  `diseases` to be a list of objects containing `name`.
- Add contract tests for `SELF`, `CAREGIVER`, relationship variants, populated
  and empty disease lists, and profile API failures.
- Decide whether profile lookup should remain best-effort or whether a failed
  lookup should be surfaced to the client for use cases that require accurate
  personalization.

### Needed if disease catalogue or disease diagnosis is a product goal

- Define the clinical scope and safety/regulatory requirements before
  implementing any diagnostic feature. The current product behavior and prompts
  explicitly prohibit diagnosis.
- If the goal is to provide context-aware education rather than diagnosis, add
  an approved, maintained medical reference source and a retrieval/citation
  design; there is no such source wired in today.
- If the product requires user-condition records in this database (the catalogue
  is already seeded), add an explicitly designed schema, ownership/consent rules,
  migrations, data validation, API operations, and tests.

### Product and engineering follow-up

- Add a web/mobile interface if users need more than the terminal client or
  direct API integration.
- Add operational checks against the live Gemini and Held services; the
  existing tests use a fake provider and do not establish live service
  availability or response quality.
- Align the existing project documentation. The root
  [`README.md`](./README.md) and [`agent/README.md`](./agent/README.md) contain
  statements that conflict with the current source (including whether
  authentication, persistence, sessions, and profile retrieval exist).
  [`document/context.md`](./document/context.md) also describes a different
  summary-storage design than the current `session_summary` and
  `last_summarized_message_id` columns.

## Repository map

- [`agent/backend/api/`](./agent/backend/api/) — FastAPI routes and session handling.
- [`agent/backend/ai_wrapper/`](./agent/backend/ai_wrapper/) — Gemini client, agents, routing, safety review, memory, and usage accounting.
- [`agent/backend/core/`](./agent/backend/core/) — configuration and Held/JWT authentication.
- [`agent/backend/db/`](./agent/backend/db/) — PostgreSQL connection and repository queries (including pgvector document search).
- [`agent/backend/rag/`](./agent/backend/rag/) — document text extraction, chunking, ingestion and retrieval for chat.
- [`agent/backend/schemas/`](./agent/backend/schemas/) — request/response models.
- [`agent/tests/`](./agent/tests/) — API, authentication, memory, safety policy, and conversation tests.
- [`document/`](./document/) — API, memory, and schema design notes; check against source where they differ.

## Overall status

| Area | Status |
| --- | --- |
| Multi-mode conversational support and safety review | Implemented |
| Authenticated sessions and stored conversation history | Implemented |
| Rolling and cross-conversation memory | Implemented |
| `SELF` / `CAREGIVER` personalization from Held profile | Implemented, dependent on external profile availability/schema |
| Profile-provided diagnosed-condition context | Partial; optional Held profile field only |
| Disease catalogue in PostgreSQL (seeded from Held) | Implemented |
| Users' own conditions in PostgreSQL | Not implemented (read live from the Held profile) |
| RAG over user-uploaded documents (pgvector) | Implemented |
| Curated medical reference retrieval | Not implemented |
| Automated disease diagnosis | Not implemented and prohibited by current prompts |
| Web/mobile user interface | Not present in this repository |
