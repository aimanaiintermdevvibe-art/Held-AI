# Held AI Database Schema

As of 2026-11-19.

The agent uses the Held app's own PostgreSQL tables. The Held app (Prisma) creates and owns them; this service creates no tables, runs no migrations and never adds users. It writes only `ChatSession` and `ChatMessage`, and only reads the rest.

| Table | Access | Use |
| :--- | :--- | :--- |
| `ChatSession` | Read, insert, update, delete | One row per chat |
| `ChatMessage` | Read, insert, update, delete | Every message and reply |
| `User` | Read | The token's user must exist here. `role` and `relation` pick the reply mode |
| `UserDisease`, `Disease` | Read | The conditions the user chose at onboarding, which pick the reply mode |
| `GriefNote` | Read | The user's grief notes, read by the grief note tools |
| `Mood` | Read | Mood titles for the grief note tools |

## `ChatSession`

| Column | Type | Default | Written by the agent |
| :--- | :--- | :--- | :--- |
| `id` | text, primary key | | A new UUID |
| `userId` | text, `User(id)` | | The token's user |
| `title` | text | `'New Conversation'` | The first 40 characters of the prompt, then a 3 to 6 word title written by the AI |
| `status` | `ChatSessionStatus` | `'ACTIVE'` | `'ACTIVE'` |
| `metadata` | jsonb | `'{}'` | Client metadata, `isNote`, and the hidden `memory` key below |
| `createdAt`, `updatedAt` | timestamp | `CURRENT_TIMESTAMP` | UTC, set explicitly |
| `sessionSummary` | text, nullable | | The rolling summary of older messages |
| `lastSummarizedMessageId` | text, nullable | | The newest message folded into the summary |
| `totalMessageCount` | integer | `0` | Raised by 2 with each saved turn |
| `lastMessageAt` | timestamp | `CURRENT_TIMESTAMP` | Not written: it keeps the chat's creation time. In the testing database only |

`userId` references `User(id)` with `ON DELETE CASCADE`.

### What `metadata` holds

| Key | Set by | Shown to the app |
| :--- | :--- | :--- |
| Client keys (for example `source`) | The `metadata` field of the first message | Yes |
| `isNote` | A chat started by a grief note | Yes |
| `memory.user_memory` | The user memory: a summary of up to 150 words of the user's recent chats, kept on their newest chat | No |
| `memory.profile` | The user's reply-mode fields (role, relation, conditions), read once per chat | No |
| `memory.safety_raised_until` | When an earlier safety concern was last checked on, so the check-in happens once per account | No |

The `memory` key is removed from every chat object the API returns, and a client cannot set it.

## `ChatMessage`

| Column | Type | Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | text, primary key | | A new UUID |
| `sessionId` | text, `ChatSession(id)` | | `ON DELETE CASCADE`: deleting a chat deletes its messages |
| `role` | text | | `user` or `assistant` (a check allows `user`, `assistant`, `system`) |
| `content` | text | | The prompt, or the final reviewed reply |
| `metadata` | jsonb | `'{}'` | The reply's token use (`usage`); `isNote` on a grief note's message |
| `createdAt` | timestamp | `CURRENT_TIMESTAMP` | UTC; the reply is stamped 1 ms after its prompt so the order is kept |

## How a turn is saved

One statement saves each turn, after the reply has passed review:

1. Inserts the user's message and the reply into `ChatMessage`.
2. Raises `ChatSession.totalMessageCount` by 2 and updates `updatedAt`.
3. For a new chat, sets the AI-written title.

If a new chat's first reply fails, the empty chat is deleted again.

## Rolling summary

The latest 20 messages of a chat are sent word for word. Older messages are folded into `sessionSummary`:

1. After each turn, the agent counts the messages after `lastSummarizedMessageId`, from the messages it already holds, with no extra read.
2. At 10 or more, a background job (in the server process) merges them into the summary: at most 250 words, in labelled parts (facts, feelings and goals, advice already given, safety answers).
3. It writes `sessionSummary` and moves `lastSummarizedMessageId` to the newest folded message. The user never waits for it.
4. If a job fails, the next turn finds the messages still waiting and tries again.

## Reads per message

| Step | Reads | Writes |
| :--- | :--- | :--- |
| Sign-in check | 1 (`User`) | |
| Existing chat | 1: the chat, its latest 20 messages, the user memory and the kept profile | 1: the turn |
| New chat | Reads of the user's recent chats (for the safety check-in and the user memory) and their profile | 1: the chat, then 1: the turn |
| Earlier-chat search | 1 full-text search, only when the user refers back to an earlier chat | |
| Grief note tools | 1 per tool call, only when the agent calls one (at most 4) | |
| Summary | 1 small read, every 10 messages | 1 |

## Not in this database

- **Indexes and full-text search indexes:** owned by the Held app. Earlier-chat search works without a dedicated index but would be faster with one.
- **A user's country:** there is none, so crisis lines come from the `SUPPORT_COUNTRY` setting (US by default).
