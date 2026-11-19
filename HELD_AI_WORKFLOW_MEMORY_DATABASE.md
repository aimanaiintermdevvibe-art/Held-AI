# Held AI: Workflow, Memory and Database

As of 2026-11-19. This repository (`Held-AI`, deployed on Render) and the private `held-ai-supabase` repository now hold the same code.

## 1. Complete workflow

`/agentic/run` and `/agentic/stream` follow the same steps for every message.

1. **Sign-in check.** The Held token is verified with `JWT_SECRET`, or confirmed by Held's `GET /auth/me`. The token's user must exist in `User`; this service never adds users.
2. **Chat.** For an existing chat, one read loads the chat, its latest 20 messages, the user memory and the kept profile. A new chat is created; a grief note sent with `isNote: true` always starts one.
3. **Profile.** The user's role, relation and conditions are read once per chat by the profile tool (`User`, `UserDisease`, `Disease`) and kept with the chat. They pick one of four reply modes.
4. **Instant crisis check.** Fixed word patterns, with no AI, in English, Spanish and Roman Urdu.
5. **Agent 1, routing.** One call picks one of five routes: crisis, grief, clinical, general support or off-topic. It also notices when the user refers back to an earlier chat.
6. **Off-topic.** A message outside mental health, wellbeing, caregiving and grief gets a short fixed reply from code, and no other agent runs. Never in a chat where the user's own risk came up.
7. **Agent 2, reasoning.** Only for clinical and grief messages.
8. **Agent 3, writing.** Writes the reply. It may call the grief note tools when the answer depends on the user's notes.
9. **Code checks.** An echoing or stock opening, a repeated safety check-in, or a safety question already answered gets one rewrite.
10. **Agent 4, review.** A light safety review. After two failed drafts the user gets a fixed safe message.
11. **Crisis line.** The first crisis reply of a chat always carries a verified crisis line; code adds it when the reply did not.
12. **Saving.** One write stores the message, the reply (with its token use) and, for a new chat, the AI-written title.
13. **Background.** Every 10 messages the chat summary is updated, and a new chat rebuilds the user memory. The user never waits.
14. **Response.** `/agentic/run` returns JSON. `/agentic/stream` sends the chat ID first, then the reviewed reply in small parts.

When Gemini cannot answer (a rate limit or an outage), a message that may show risk gets a fixed crisis reply with 988 and 911; other messages get an error (`429`, `503`, `504`).

| Setting | Value |
| :--- | :--- |
| AI model | `gemini-3.5-flash-lite` for every agent |
| Thinking level | Low for Agents 1, 2 and 4; medium for Agent 3 |
| Reply length limit | 8,192 tokens per AI call, thinking included |
| Retries | Up to 3 tries after a passing failure (5xx, timeout, empty reply); a rate limit with a wait of 10 seconds or less is waited out once |
| Time limit | 240 seconds per message |

### Endpoints

| Endpoint | Purpose |
| :--- | :--- |
| `POST /agentic/run`, `POST /agentic/stream` | Send a message |
| `GET /`, `GET /agentic/health` | Public checks |

The app lists chats, shows a chat's messages, and archives or deletes chats through Held's own API (`/chat-sessions`), which reads the same `ChatSession` and `ChatMessage` rows. This service only answers messages.

Full reference: [document/api.md](./document/api.md).

Code: `agent/backend/ai_wrapper/orchestrator.py`, `agent/backend/api/routers/agentic.py`

## 2. How context memory works

Memory is rebuilt from the database for every message, from at most one read.

- **Current chat:** the latest 20 messages word for word. A message over 4,000 characters keeps its first and last 2,000.
- **Long chat:** once a chat has more than 20 messages, a summary of up to 250 words is added, in labelled parts: facts, feelings and goals, advice already given, and safety answers. It updates in the background every 10 messages.
- **New chat:** starts fresh. It gets no recaps of earlier chats, only the user memory as silent background (up to 150 words, never mentioning suicide, self-harm or thoughts of dying).
- **Recall:** when the user refers back to an earlier chat, a full-text search over both sides of their chats from the last 365 days gives the writer the top 5 excerpts, with safety talk removed.
- **Safety check-in:** a safety concern from a chat in the last 6 days is checked on once per account, in the first reply of a new chat, only when the user talks about how they feel and never in a reply about a new loss.

| Agent | Memory it gets |
| :--- | :--- |
| Agent 1, routing | Summary, the user's messages among the latest 20, the last 6 messages in full |
| Agent 2, reasoning | Summary and the last 10 messages |
| Agent 3, writing | Everything above, plus the user memory and recalled excerpts |
| Agent 4, review | Everything except the agent's own earlier replies |

- **Other chats stay out of routing:** Agents 1 and 2 never see them.
- **Memory is referred to tentatively**, and only when the latest message is about the same thing.
- **A memory list leaves out** abuse, suicidal thoughts and self-harm.
- **Crisis detection** still runs on every message, so new risk is always caught.

More: [document/context.md](./document/context.md). Code: `agent/backend/ai_wrapper/memory.py`

## 3. Tables in the database

The Held app's own tables (Prisma), in the `public` schema. The agent creates none.

| Table | Access | Holds |
| :--- | :--- | :--- |
| `ChatSession` | Read and write | One row per chat: title, summary, message count, metadata |
| `ChatMessage` | Read and write | Every message and reply |
| `User` | Read | The signed-in users; role and relation |
| `UserDisease`, `Disease` | Read | The conditions chosen at onboarding |
| `GriefNote` | Read | The user's grief notes |
| `Mood` | Read | Mood titles |

Links: `ChatSession.userId` → `User` and `ChatMessage.sessionId` → `ChatSession`, both `ON DELETE CASCADE`.

Columns and metadata keys: [document/scheme.md](./document/scheme.md).

## 4. How data is stored

- **Chats:** a new chat creates a `ChatSession` row. Each turn adds 2 `ChatMessage` rows in the same write that updates the chat. If a new chat's first reply fails, the empty chat is deleted. Deleting a chat removes its messages.
- **Hidden memory:** the user memory, the kept profile and the safety check-in mark live in `ChatSession.metadata.memory`, which the API never returns.
- **Grief notes:** read only, from `GriefNote` and `Mood`, by the grief note tools. Notes are written by the Held app.
- **Times:** UTC, with millisecond precision. A reply is stamped 1 ms after its message.
- **Stored outside the database:** the settings in `.env` (git-ignored). Nothing else.

## 5. Database calls and tokens per message

| Database calls | Count |
| :--- | :--- |
| Sign-in check | 1 read |
| Existing chat | 1 read, 1 write |
| New chat | Reads of recent chats and the profile, 1 write for the chat, 1 for the turn |
| Earlier-chat search | 1 read, only when the user refers back |
| Grief note tools | 1 read per call, only when the agent calls one |
| Summary | 1 read and 1 write per 10 messages |

Measured on 2026-11-19 with real Gemini calls, for the first message of a chat:

| Route | AI calls | Input tokens | Estimated cost |
| :--- | :--- | :--- | :--- |
| General support | 3 | 10,840 | $0.0056 |
| Clinical question | 4 | 11,608 | $0.0068 |
| Grief | 4 | 11,277 | $0.0065 |
| Crisis (instant check) | 2 | 9,429 | $0.0073 |
| Off-topic | 1 | 1,416 | $0.0006 |

A new chat adds one call for its title. Longer chats cost about the same, because at most 20 messages are sent. Every turn logs a line such as `usage turn session=... calls=4 input=11608 output=517 thinking=792 cost_usd=...`.

## 6. Reply quality check

The smaller memory (20 messages plus summary) was compared with sending the whole chat on 2026-11-04, on one 44-message test chat and one crisis chat. Each question ran 3 times, and a separate Gemini call judged each pair without knowing which was which.

| Check | Whole chat | Current memory |
| :--- | :--- | :--- |
| Crisis follow-up a few turns later ("that feeling is back") | 6/6 | 6/6 |
| Old advice repeated as new | 0/3 | 0/3 |
| Early detail recalled | 3/3 | 3/3 |
| Judge's preference against the whole chat | - | Won 7 of 12 |

This is one test chat with a model as judge: a strong sign rather than proof.
