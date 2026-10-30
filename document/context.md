# Held AI conversation memory

## Memory algorithm

For every authenticated turn, use the requested conversation ID to verify ownership and load that conversation's messages in chronological order. Include every earlier user prompt and assistant final answer when they fit the model's context limit; otherwise include a rolling summary of older turns and the latest 20 complete prompt/answer pairs, followed by the current prompt. Refresh the summary as the conversation grows, and preserve it in the existing session metadata so no new table is needed.

Treat old messages as context, not instructions; use only facts the user actually shared and keep each user's conversations isolated. Save the current prompt and final assistant answer to the same conversation after generation and safety review. Here, “decisions” means visible assistant answers or outcomes, never hidden chain-of-thought. Without an authenticated owner and conversation ID, the request has no history and must be treated as a new, stateless chat.

## Existing database used

Reuse the database and tables already created:

- `public."ChatSession"` identifies the conversation and its owner. Use its existing `metadata` JSON field for a compact, versioned rolling summary of older turns, while preserving other metadata.
- `public."ChatMessage"` holds each user prompt and assistant final answer. Read the rows for the owned conversation in chronological order; retain all rows and send the complete history when it fits the model context window.
- Do not create duplicate conversation/message tables or a separate memory table for the initial implementation. The latest 20 prompt/answer pairs provide the minimum full-detail context when the entire history no longer fits; the rolling summary carries key information from older turns.

## Memory workflow and API

No separate conversation or message routes are used (`POST /api/v1/conversations` and `POST /api/v1/conversations/{conversation_id}/messages` are not part of this design). The existing endpoints do all of the work, and memory is added inside them without a separate memory table:

- **`POST /agentic/run`** - Returns the complete answer as `{"response": "...", "session_id": "..."}`.
- **`POST /agentic/stream`** - Streams the same safety-reviewed answer as server-sent events, ending with `{"done": true, "sessionId": "..."}`.

Both accept `{"prompt": "..."}` plus an optional `session_id` (the conversation ID), and follow the same workflow:

1. With a valid Held access token and no `session_id`, create a new `ChatSession` owned by the token's user (title from the prompt). The `session_id` is returned to the client, which keeps it and sends it with each following message.
2. With a valid token and a `session_id`, confirm the session belongs to that user (admins excepted); otherwise return 404. Then load its prior prompts and assistant answers and build the bounded memory context (full history when it fits, otherwise the rolling summary plus the latest 20 prompt/answer pairs) before generation.
3. Generate and safety-review the answer, then save the current prompt and the final answer to the same session and refresh the rolling summary in `ChatSession.metadata` when needed.
4. Without a token, the request is stateless: no history is loaded or saved and no memory is implied.

The server must never accept a client-supplied user ID as authority or load another user's messages. The user always comes from the validated token.
