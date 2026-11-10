# Held AI: Workflow, Memory and Database

As of 2026-11-04.

> **Status:** this week's changes are only on the local PC. They are not committed, so Render still runs the older version, where RAG is on, the Supervisor still runs and memory sends the whole chat. Migration 006 (the `user_memory` table) is already applied to Supabase; the old version ignores it.

## 1. Complete workflow

`/agentic/run` and `/agentic/stream` follow the same steps for every message.

1. **Login check.** The token is verified with `JWT_SECRET`, or by asking Held (`/auth/me`). A Held user who isn't in the database yet is added to `User`. With `AUTH_DISABLED=true`, as on Render now, everyone uses one shared guest account.
2. **Chat session.** For an existing chat, one database read loads the session, its latest 20 messages and the user memory. A new chat is created, and recaps of the user's 10 most recent other chats are read.
3. **Context.** It builds memory, gets the user's Held profile to set their mode, and adds grief notes if the message mentions a journal or notes.
4. **Safety and routing.** An instant crisis check runs first, with no AI. Then Agent 1 picks one of 5 routes in a single call: off-topic, general support, clinical, grief or crisis.
5. **Reasoning.** Agent 2 runs only for clinical and grief messages. Clinical uses its DSM-5-TR knowledge, and grief uses a companioning approach.
6. **Writing.** Agent 3 writes the reply.
7. **Code checks.** The opening line and repeated safety questions are checked, and a bad draft gets one rewrite.
8. **Review.** Agent 4 does a light safety review. If the draft fails twice, the user gets a fixed safe message.
9. **Saving.** One database write stores the user's message, the reply, the message count and, for a new chat, its AI-written title. The token use is logged.
10. **Background.** Every 10 messages the chat summary is updated. When a new chat starts, the user memory is rebuilt. The user never waits for either.
11. **Response.** `/agentic/run` returns JSON. `/agentic/stream` sends the session ID first, then the reviewed reply in small parts.

| Setting            | Value                                                |
| ------------------ | ---------------------------------------------------- |
| AI model           | `gemini-3.5-flash-lite` for every agent              |
| Thinking level     | Low for Agents 1, 2 and 4; medium for Agent 3        |
| Document RAG       | Commented out locally                                |
| Reply length limit | 8,192 tokens per AI call, thinking included          |
| Retries            | Up to 3 tries after a passing failure or empty reply |

### Other endpoints

| Endpoint                              | Purpose                              |
| ------------------------------------- | ------------------------------------ |
| `GET /`                               | Public check that the app is up      |
| `GET /agentic/health`                 | Public check of the app and database |
| `GET /agentic/sessions`               | List chats                           |
| `GET /agentic/sessions/{id}/messages` | Read one chat                        |
| `DELETE /agentic/sessions/{id}`       | Delete one chat                      |
| `POST /agentic/users/register`        | Add the signed-in Held user          |
| `GET /agentic/users`                  | List users (admin only)              |
| `GET /agentic/users/{id}`             | Get one user (admin only)            |
| `GET /agentic/admin/overview`         | Row counts (admin only)              |
| `GET /agentic/diseases`               | Condition list                       |
| `/agentic/grief-notes/...`            | Grief notes and moods                |

Code: `agent/backend/ai_wrapper/orchestrator.py`, `agent/backend/api/routers/agentic.py`

## 2. How context memory works

Memory is rebuilt from the database for every message, from at most one read.

- **Current chat:** the latest 20 messages word for word. A message over 4,000 characters keeps its first and last 2,000.
- **Long chat:** once a chat has more than 20 messages, a summary of up to 250 words is added, in labelled parts: facts, feelings and goals, and the advice the bot already gave. It updates in the background every 10 messages and is saved on the session.
- **Other chats, first reply of a new chat:** the 10 most recent, with title, summary and the user's last 6 messages (600 characters each). The bot's own replies are left out.
- **Other chats, later replies:** the user memory, a summary of up to 150 words of those same recaps, stored in `user_memory`. It is rebuilt in the background each time a new chat starts.
- **Safety concerns:** risk messages from other chats, up to 3 per chat, used only if under 6 days old and only for the first reply of a new chat. Otherwise they're removed from memory, including matching sentences in summaries. The user memory never mentions suicide, self-harm or thoughts of dying.
- **Grief notes:** the 10 newest notes, 1,000 characters each, only when the message mentions notes, a journal, diary or workbook.

| Agent              | Memory it gets                                             |
| ------------------ | ---------------------------------------------------------- |
| Agent 1, routing   | Summary, user's messages in the latest 20, last 6 messages |
| Agent 2, reasoning | Chat summary and the last 10 messages of this chat         |
| Agent 3, writing   | Everything above                                           |
| Agent 4, review    | Everything except the bot's own earlier replies            |

- **Other chats stay out of routing:** Agents 1 and 2 never see them, so an old chat can't turn a new message into a crisis.
- **Past hardships:** the bot mentions a past loss or diagnosis only if the user's latest message raises it.
- **Repeat guard:** code stops the safety question from being asked twice in one chat. In a long chat, the summary records that it was asked.
- **No repeated advice:** Agent 3 treats the summary's "Already suggested" list as advice already given.
- **Crisis detection:** still runs on every message, so new risk is always caught.
- **Shared memory on Render:** while guest mode is on, all testers share one memory.

Code: `agent/backend/ai_wrapper/memory.py`

## 3. Tables in the database

The app has 9 tables, all in the `public` schema, all created by the project's migrations in `agent/migrations/`.

| Table             | Rows now | Added by | Holds                                  |
| ----------------- | -------- | -------- | -------------------------------------- |
| `User`            | 6        | 001      | Users copied from Held, plus the guest |
| `chat_sessions`   | 65       | 001      | One row per chat, with its summary     |
| `chat_messages`   | 282      | 001      | Every message and reply                |
| `documents`       | 1        | 002      | The DSM-5-TR upload record             |
| `document_chunks` | 6,988    | 002      | Book chunks with embeddings            |
| `Disease`         | 21       | 003      | Condition list from Held               |
| `Mood`            | 20       | 005      | Mood list from Held                    |
| `grief_notes`     | 5        | 005      | Copies of grief notes                  |
| `user_memory`     | 0        | 006      | One short memory per user              |

**New tables:** 6 were added on top of the original 3 (`User`, `chat_sessions`, `chat_messages`): `documents`, `document_chunks`, `Disease`, `Mood`, `grief_notes` and `user_memory`. Migration 004 only added columns to `documents`.

**Links between tables:**

| From                          | To              |
| ----------------------------- | --------------- |
| `chat_sessions.user_id`       | `User`          |
| `chat_messages.session_id`    | `chat_sessions` |
| `documents.user_id`           | `User`          |
| `document_chunks.document_id` | `documents`     |
| `grief_notes.user_id`         | `User`          |
| `user_memory.user_id`         | `User`          |

**Other database items:** 4 enum types (`AuthProvider`, `OAuthProvider`, `RelationType`, `UserRole`) and the `vector` extension, version 0.8.2, for embeddings.

## 4. How data is stored

- **Chats:** a new chat creates a `chat_sessions` row. Each turn adds 2 `chat_messages` rows, one for the user and one for the reply, in the same write that updates the session. If a new chat's first reply fails, the empty chat is deleted. Deleting a chat removes its messages too.
- **User memory:** one `user_memory` row per user, replaced each time they start a new chat. Deleting a user removes it.
- **Users:** a row is added from the Held profile the first time someone logs in. No password is stored, and existing rows are never changed.
- **Grief notes:** a note is its text and a mood. Held is the main copy; the backend saves a copy and allows one note per user per date. Held has no delete for notes.
- **Lists:** `Disease` and `Mood` are filled from Held by seed scripts in `agent/scripts/`.
- **DSM book:** the 6,988 chunks are still stored but not used now that RAG is off.
- **Security:** row level security is on for all 9 tables, so Supabase's public API keys can't read them. Only the backend, using `DATABASE_URL`, can.
- **Stored outside the database:** test logins and tokens in `users/*.json` and `.env`, and upload logs in `logs/`. These are all on the local PC only and are not tracked by git.

## 5. Database calls and tokens per message

Measured on 2026-11-04 with real Gemini calls and the live database.

| Database calls          | Before              | After                            |
| ----------------------- | ------------------- | -------------------------------- |
| Existing chat           | 3 reads, 2 writes   | 1 read, 1 write                  |
| New chat, first message | 1 read, 4 writes    | 1 read, 2 writes                 |
| Summary check           | 2 reads per message | 1 read + 1 write per 10 messages |
| User memory             | None                | 1 write per new chat             |

Each request also has its login check, 1 read, as before. The old reads grew with the chat, because every message of it was read twice; the new read is never more than 20 messages.

| Input tokens per message | Before             | After |
| ------------------------ | ------------------ | ----- |
| New chat, first message  | 9,802              | 9,849 |
| 20 earlier turns         | 23,190             | 9,485 |
| 220 earlier turns        | 152,767            | 9,490 |
| 1,000 earlier turns      | Over the old limit | 9,469 |

A message now costs about $0.005 to $0.009 however long the chat is; it was $0.048 near the old limit. Agent 3's instructions, about 4,500 tokens, are most of what remains. Every turn logs a line such as `usage turn session=... calls=3 input=9485 output=265 thinking=988 cost_usd=0.00600`.

## 6. Reply quality check

The smaller memory was compared with the old whole-chat memory on 2026-11-04: one 44-message test chat and one crisis chat, each question run 3 times, with a separate Gemini call judging each pair of replies without knowing which was which.

| Check                              | Whole chat | First cut     | Final       |
| ---------------------------------- | ---------- | ------------- | ----------- |
| Crisis follow-up a few turns later | 6/6        | 0/6           | 6/6         |
| Old advice repeated as new         | 0/3        | 2/3           | 0/3         |
| Early detail recalled              | 3/3        | 3/3           | 3/3         |
| Judge vs old memory                | -          | lost 11 of 12 | won 7 of 12 |

The first cut showed only the last 6 messages to the router and kept no list of advice already given, so two changes were added: the router now also reads the user's earlier messages, and the summary lists what was already suggested. This is one test chat and a model as judge, so it is a strong sign rather than proof.
