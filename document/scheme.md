# Held Shared Database Schema

This document details the PostgreSQL database schema for Held AI chat sessions and messages, including the rolling context memory and background summarisation design.

---

## 1. DDL Schema Definition

```sql
-- 1. Session Table (Holds state & anchor pointer)
CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    session_summary TEXT DEFAULT NULL,
    last_summarized_message_id UUID DEFAULT NULL, -- Anchor point
    total_message_count INT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. Messages Table
CREATE TABLE chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL, -- 'user' or 'assistant'
    content TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_chat_messages_session ON chat_messages(session_id, created_at ASC);
```

---

## 2. Table Specifications

### `public.chat_sessions`

Holds high-level session state, rolling session summaries, and the summarisation anchor pointer.

| Column | PostgreSQL Type | Nullable | Default / Constraints | Description |
| --- | --- | --- | --- | --- |
| `id` | `UUID` | No | `PRIMARY KEY DEFAULT gen_random_uuid()` | Unique session identifier |
| `user_id` | `UUID` | No | None | Identifier of the user owning the session |
| `session_summary` | `TEXT` | Yes | `DEFAULT NULL` | Compact rolling summary of the conversation history (≤ 250 words) |
| `last_summarized_message_id` | `UUID` | Yes | `DEFAULT NULL` | Anchor point: ID of the latest message incorporated into `session_summary` |
| `total_message_count` | `INT` | Yes | `DEFAULT 0` | Running total count of all messages (user + assistant) in this session |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | Yes | `DEFAULT NOW()` | Session creation timestamp with timezone |

---

### `public.chat_messages`

Stores individual conversation turns for each session in chronological order.

| Column | PostgreSQL Type | Nullable | Default / Constraints | Description |
| --- | --- | --- | --- | --- |
| `id` | `UUID` | No | `PRIMARY KEY DEFAULT gen_random_uuid()` | Unique message identifier |
| `session_id` | `UUID` | Yes | `REFERENCES chat_sessions(id) ON DELETE CASCADE` | Parent session foreign key |
| `role` | `VARCHAR(20)` | No | None | Message sender role: `'user'` or `'assistant'` |
| `content` | `TEXT` | No | None | Message text content |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | Yes | `DEFAULT NOW()` | Message timestamp with timezone |

#### Indexes

| Index Name | Target Table | Columns | Purpose |
| --- | --- | --- | --- |
| `idx_chat_messages_session` | `chat_messages` | `(session_id, created_at ASC)` | Fast chronological retrieval of session message history |

---

## 3. Context Memory & Summarisation Mechanism

### State & Anchor Pointer Strategy

Rather than storing summary states in nested JSONB blobs, `chat_sessions` stores summary state directly in dedicated relational columns:

- **`session_summary`**: Holds the ongoing LLM-generated rolling summary. It starts as `NULL` until the first summarisation threshold is met.
- **`last_summarized_message_id`**: Serves as the anchor pointer. It stores the UUID of the most recent message that was compressed into `session_summary`. When generating the next summary chunk, only messages created after this anchor ID need to be retrieved and merged.
- **`total_message_count`**: Increments with each message persisted to quickly evaluate whether a threshold has been crossed without costly `COUNT(*)` queries.

### 10-Message Threshold Summarisation Flow

```
┌─────────────────────────────────────────────────────────────────┐
│ Per Turn (User prompt + Assistant response):                    │
│                                                                 │
│  1. INSERT INTO chat_messages (session_id, role, content)       │
│     ├── Role: 'user'                                            │
│  2. UPDATE chat_sessions SET total_message_count = count + 1    │
│  3. INSERT INTO chat_messages (session_id, role, content)       │
│     ├── Role: 'assistant'                                       │
│  4. UPDATE chat_sessions SET total_message_count = count + 1    │
│  5. Enqueue background summary check (async/non-blocking)       │
│                                                                 │
│ Background Consumer (Worker):                                   │
│  6. Check unsummarized message threshold:                       │
│     • Count unsummarized messages since                         │
│       `last_summarized_message_id`                              │
│     • If unsummarized count >= 10:                              │
│         - Fetch messages after `last_summarized_message_id`     │
│         - LLM generates updated rolling summary                 │
│         - UPDATE chat_sessions SET                              │
│             session_summary = <new_summary>,                    │
│             last_summarized_message_id = <latest_message_id>    │
│     • Else: No-op, skip update                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Context Window at Inference Time

When preparing history for agent execution:

| Condition | Context Provided to LLM |
| --- | --- |
| **Within token/character budget** | Full chronological history from `chat_messages` |
| **Exceeds token/character budget** | Current `session_summary` + recent prompt/response pairs (or messages following `last_summarized_message_id`) |

### Core Design Principles

1. **Non-blocking UX**: Summarisation tasks are executed asynchronously in background jobs; the user is never blocked during message generation.
2. **Deterministic Anchoring**: Using `last_summarized_message_id` ensures that summary updates only process new unsummarised messages, preventing duplicated summary context.
3. **Cascade Deletes**: Deleting a session in `chat_sessions` automatically removes all associated rows in `chat_messages`.
4. **Resilient & Idempotent**: If a background summarisation job fails or is interrupted, the next turn detects that unsummarised messages remain $\ge 10$ and retries.
