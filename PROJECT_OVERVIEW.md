# Held AI Chatbot: Project Overview

As of 2026-11-19.

## Executive summary

This repository is the Python (FastAPI) backend of the Held app's AI companion for mental health, caregiving and grief. Google Gemini writes the replies through a four-agent pipeline with a final safety review. Users sign in with Held access tokens, and every conversation is saved in the Held app's own PostgreSQL tables.

It is a backend only: the Held mobile app is the user interface. The chatbot gives support and education; it does **not** diagnose, prescribe or replace professional care, and it says so when asked.

## What is implemented

### Replies

Each message is routed by Agent 1 and written by Agent 3, then reviewed by Agent 4 ([orchestrator.py](./agent/backend/ai_wrapper/orchestrator.py)).

| Route | Purpose |
| :--- | :--- |
| `general_support` | Everyday emotional support |
| `clinical_support` | Education about mental-health conditions and caregiving, after Agent 2's clinical reading |
| `grief` | A companion's reply to loss, never treating grief as an illness |
| `crisis` | Safety first: a step-by-step risk protocol and verified crisis lines |
| `off_topic` | Anything outside mental health, wellbeing, caregiving and grief: a short fixed reply, with no answer written |

Replies read like a caring person, not a script: no stock sympathy lines, short paragraphs, bold questions, bullets and numbered steps where they help. Code checks the opening line and repeated safety questions.

### Safety

- **Instant crisis check** before any AI runs, in English, Spanish and Roman Urdu ([policy.py](./agent/backend/ai_wrapper/agents/agent1_intent_guardrail/policy.py)).
- **Crisis protocol:**
  - asks about thoughts of ending their life, then a plan, access to means, and timing, one question at a time;
  - puts safety actions first when danger is immediate;
  - encourages distance from the means.
- **Verified support lines** in [support_resources.py](./agent/backend/ai_wrapper/support_resources.py):
  - the US lines: 988, 911, Poison Control, the National Domestic Violence Hotline, Crisis Text Line, the ANAD helpline and suicide-loss support;
  - the agents may give no other number;
  - the first crisis reply of a chat always carries one.
- **Never unanswered:** when Gemini is unavailable, a message that may show risk gets a fixed crisis reply with 988 and 911.
- **Hard limits:**
  - no lethal amounts, methods or ways to hide self-harm, however the request is framed;
  - "I", "me" and "my" always mean the user, also for a caregiver.

### Reply modes from the user's profile

Four modes, from the user's onboarding answers: `myself_diagnosed`, `myself_undiagnosed`, `caregiver_diagnosed` and `caregiver_undiagnosed`.

- **Read from:** `User.role` and `relation`, and the conditions in `UserDisease` and `Disease`, through the read-only profile tool ([user_profile.py](./agent/backend/tools/user_profile.py)).
- **When:** read once per chat and kept with it (hidden from the app). Held's `GET /auth/me` is the fallback.
- **Effect:** the mode guides reasoning, writing and review. An undiagnosed mode never suggests a condition.

### Memory

- **Within a chat:** the latest 20 messages, plus a rolling summary updated in the background every 10 messages.
- **New chat:** starts fresh, with a short user memory as silent background.
- **Recall:** when the user refers back to an earlier chat, a full-text search finds what they said.
- **Safety check-in:** an earlier safety concern is checked on once per account.
- **Privacy:** memory is referred to tentatively, and a "what do you remember" list leaves out abuse, suicidal thoughts and self-harm.

See [document/context.md](./document/context.md).

### Grief notes

- **Note tools:** in a chat, Agent 3 may call three read-only tools on the user's own notes: `recent_notes`, `note_by_date` and `mood_summary`.
  - The user comes only from the token, and arguments are validated and limited.
  - Note text is data, never instructions, and crisis language in a note takes the crisis path.
- **Sending a note:** a note the app sends with `isNote: true` starts its own chat and gets a grief companion's reply.

### API

`POST /agentic/run` (JSON) and `POST /agentic/stream` (server-sent events), and public health checks. The app's chat list and history come from Held's own API (`/chat-sessions`). See [document/api.md](./document/api.md).

## Database and external services

| Service | Use |
| :--- | :--- |
| Held PostgreSQL (`DATABASE_URL`) | Writes `ChatSession` and `ChatMessage`; reads `User`, `UserDisease`, `Disease`, `GriefNote` and `Mood`. Creates no tables and never adds users. See [document/scheme.md](./document/scheme.md) |
| Held API (`HELD_API_BASE_URL`) | Confirms a token that `JWT_SECRET` cannot verify, and is the profile fallback |
| Google Gemini (`GEMINI_API_KEY`) | All four agents, chat titles and summaries. A paid key is needed in production |

Deployments:
- **Render:** runs this repository's `main` branch on the testing Supabase database (Held app tables). Deploys are manual.
- **Company LAN:** the private `held-ai-supabase` repository holds the same code and is used for local testing on the company database.

## What remains

### Before release

- A clinician should review a sample of conversations, especially crisis replies and the 6-day limit for safety check-ins.
- Confirm the crisis lines in `support_resources.py` again, and add lines for each country the app launches in. The database has no country, so `SUPPORT_COUNTRY` sets it.
- Rotate the database password and secrets that were shared during testing.
- Add rate limiting per user.

### App work (Flutter)

- Treat a stream event with `status` as an error: show it, offer Retry, and keep the user's text.
- A "What Held remembers" screen to view, delete or turn off memory (needs new endpoints).
- An app lock or PIN before chat opens.

### Engineering

- A full-text index for earlier-chat search.
- Move the background summary jobs out of the web process if traffic grows.
- Turn the safety audit's 63 scenarios into an automated regression test run before every prompt or model change.

## Repository map

| Path | Contents |
| :--- | :--- |
| [`agent/backend/api/`](./agent/backend/api/) | FastAPI routes |
| [`agent/backend/ai_wrapper/`](./agent/backend/ai_wrapper/) | The four agents and their prompts, the Gemini client, routing, memory, support lines and token logging |
| [`agent/backend/core/`](./agent/backend/core/) | Settings and Held token authentication |
| [`agent/backend/db/`](./agent/backend/db/) | Connection pool and queries on the Held tables |
| [`agent/backend/grief/`](./agent/backend/grief/) | The grief note tools |
| [`agent/backend/tools/`](./agent/backend/tools/) | The profile tool |
| [`agent/backend/schemas/`](./agent/backend/schemas/) | Request and response models |
| [`agent/tests/`](./agent/tests/) | 432 tests; they use an in-memory stand-in, so they need no database or Gemini |
| [`document/`](./document/) | API, memory and schema reference |

## Overall status

| Area | Status |
| :--- | :--- |
| Five routes with safety review | Done |
| Crisis protocol, verified support lines, answer during outages | Done (US lines) |
| Scope limit (off-topic declined) | Done |
| Signed-in chats saved in the Held tables | Done |
| Chat summary, user memory and earlier-chat recall | Done |
| Four reply modes from the Held profile | Done |
| Grief note tools and notes sent from the app | Done |
| Support lines for other countries | Not yet |
| Memory screen, app lock, stream error handling in the app | App work, not started |
| Diagnosis or prescribing | Not offered, by design |
