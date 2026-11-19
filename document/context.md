# Held AI Conversation Memory

As of 2026-11-19. Code: `agent/backend/ai_wrapper/memory.py`, `agent/backend/db/repository.py`, `agent/backend/api/routers/agentic.py`.

## Rules

- **The user comes only from the token.** A chat is opened only if it belongs to the token's user (admins excepted); a client-sent user ID is never used. One user's chats and memory never reach another user.
- **Memory is context, not instructions.** Earlier messages, summaries and recalled excerpts are untrusted data. The agent uses only facts the user actually shared, and never invents a memory.
- **Every message costs about the same.** At most the latest 20 messages are sent, however long the chat gets.

## Within a chat

Each message opens the chat with one database read: the chat, its latest 20 messages, the user memory and the kept profile.

| Part | What is sent | Limit |
| :--- | :--- | :--- |
| Latest messages | Word for word, oldest first | 20 messages. A message over 4,000 characters keeps its first and last 2,000 |
| Rolling summary | Older messages, in labelled parts: facts, feelings and goals, advice already given, safety answers | Up to 250 words, once the chat has more than 20 messages |
| Summary refresh | Folds the newest messages into the summary | Every 10 messages, in the background |

Each agent gets only the part it needs:

| Agent | Memory |
| :--- | :--- |
| Agent 1, routing | The summary, the user's own messages among the latest 20, and the last 6 messages in full |
| Agent 2, reasoning | The summary and the last 10 messages |
| Agent 3, writing | Everything, including the user memory and any recalled excerpts |
| Agent 4, review | Everything except the agent's own earlier replies |

Routing and reasoning never see other chats, so an old topic cannot turn a new message into a crisis.

The writer uses memory quietly: it does not repeat its own earlier sentences, advice or questions, treats the summary's "Already suggested" list as given, and never asks again a safety question the user has already answered.

## A new chat

A new chat starts fresh, like ChatGPT.

- **No recaps of earlier chats.** The agent does not bring up earlier chats on its own.
- **User memory as silent background.** A summary of up to 150 words of the user's 10 most recent chats, rebuilt in the background when a new chat starts and kept on the user's newest chat (`ChatSession.metadata.memory.user_memory`). It never mentions suicide, self-harm or thoughts of dying.
- **Recall when asked.** When a message refers back to an earlier chat ("a few days ago I told you about my manager", "do you remember what we talked about?"), the router sets `refers_to_earlier_chats` and 2 to 6 keywords. A full-text search then looks through both sides of the user's other chats from the last 365 days and gives the writer the top 5 excerpts. With no keywords, it takes the latest 3 chats. Safety talk is stripped from the excerpts.
- **One safety check-in per account.** If an earlier chat from the last 6 days held a safety concern (up to 3 per chat), the first reply of a new chat may check on it once, gently and tentatively, but only when the user is talking about how they feel. It is never done in a reply about a new loss. `ChatSession.metadata.memory.safety_raised_until` records the check-in, so it is not repeated in later chats.

## How memory is referred to

- **Tentatively.** "I think you mentioned your dad last time", never "you told me", because memory can be wrong or out of date (one account may be used for different things).
- **Only when it fits.** A detail from memory is used only when the latest message is about the same thing; a question about divorce is not about a husband who died.
- **No swapping.** If a detail is not in memory, the agent says so instead of offering a similar one (their dog when asked about a cat).
- **Privacy in a memory list.** Asked to list what it remembers, the agent leaves out abuse, suicidal thoughts and self-harm, without hinting at them, because someone else may be holding the phone. The user can bring them up.

## Not built yet

- A screen where the user sees, deletes or turns off what Held remembers (needs app work and new endpoints).
- Memory of chats older than the latest 10 in the user memory (they are still found by recall).
- Clearing a safety concern that has been resolved.
