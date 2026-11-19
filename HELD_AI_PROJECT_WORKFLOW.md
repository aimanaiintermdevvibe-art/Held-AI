# Held AI Project Workflow

As of 2026-11-19

## Summary

Held AI is the Held app's support companion for mental health, caregiving and grief. Every message passes an instant safety check, up to four AI agents and a final safety review before the user sees a reply.

- **Running:** the backend is Python (FastAPI) on Render, using the Held app's own PostgreSQL tables and a paid Gemini key. Every request needs a Held access token; guest mode is gone.
- **Tested:** all 432 automated tests pass. Two safety audits by the developer (63 scenarios, two accounts) were fixed and re-run live.
- **Scope:** Held talks only about mental health, wellbeing, caregiving and grief, and declines anything else.

## Overall workflow

```text
Message arrives
POST /agentic/run or /agentic/stream, with a Held token
        |
        v
Context is loaded
Sign-in check, the chat and its memory, the user's profile (read once per chat)
        |
        v
Instant crisis check ------------------ crisis ---------------> CRISIS route
Word patterns, no AI (English, Spanish, Roman Urdu)
        |
        | no alert
        v
Agent 1: routing ---------------------- crisis ---------------> CRISIS route
One call picks one of five routes ----- off-topic ------------> Fixed reply from code, nothing else runs
(and notices references to                                      (not in a chat where risk came up)
earlier chats)
        |
        v
  GENERAL SUPPORT  Warm reply, no Agent 2
  CLINICAL         Agent 2 gives a clinical reading
  GRIEF            Agent 2 gives a companioning read
  CRISIS           Skips Agent 2, crisis protocol
        |
        v
Agent 3: writes the reply <---------------------------------+
May call the grief note tools; uses the mode, memory and    |
the verified support lines                                  |
        |                                                   |
        v                                                   |
Code checks: echoing or stock opening, repeated check-in,   |
a safety question already answered -> one rewrite ----------+
        |                                                   |
        v                                                   |
Agent 4: safety review                                      |
Repairs only real problems; if it fails, one more try ------+
        |
        +-- passes ----------> Crisis reply? code makes sure it carries a crisis line
        |                      Reply reaches the user; saved in one write;
        |                      summary and user memory update in the background
        |
        +-- second failure --> Fixed safe message (with 988 and 911 in a crisis)

Gemini unavailable (rate limit, outage)?
  a message that may show risk -> fixed crisis reply with 988 and 911
  any other message            -> an error (429, 503, 504) the app should show with Retry
```

`/agentic/stream` runs the same steps, then sends the finished, reviewed reply in small parts.

Code: `agent/backend/ai_wrapper/orchestrator.py`, `agent/backend/api/routers/agentic.py`

## User-based modes

The mode comes from two onboarding answers: who the user is chatting for, and whether that person has a diagnosis.

| Mode | Chatting for | Diagnosis on record | How replies change |
| --- | --- | --- | --- |
| `myself_diagnosed` | Themselves | Yes | Treats the diagnosis as known. Does not explain or question it. Links support to it only when the message relates to it. |
| `myself_undiagnosed` | Themselves | No | Never suggests or guesses a condition. Uses everyday words. If asked, says only a qualified clinician can assess this. |
| `caregiver_diagnosed` | Someone they care for | Yes | Speaks as a practical partner in care, keeping the caregiver's own strain separate. |
| `caregiver_undiagnosed` | Someone they care for | No | Helps with what the caregiver notices, without labelling the person. |

### How the mode is chosen

1. When a chat starts, the profile tool reads the user's `role` and `relation` from `User`, and their conditions from `UserDisease` and `Disease`. It is bound to the token's user and returns nothing else (no name, email or ID).
2. `role` decides myself (`SELF`) or caregiver (`CAREGIVER`). No conditions means undiagnosed.
3. The result is kept with the chat (hidden from the app) and reused by its later messages. A new chat reads it afresh, so a profile change applies from the next chat.
4. If the database cannot give it, Held's `GET /auth/me` is the fallback.

### "I" is always the user

A caregiver also writes about their own life, health and safety. "I took 20 of my lithium pills" is the user's own overdose, even on a caregiver account. Agents 2, 3 and 4 follow this rule, and the caregiver context tells the agents so. The first safety audit found this as its most dangerous issue; it is fixed and re-tested.

| Step | Uses the mode? |
| --- | --- |
| Agent 1 (routing) | No: routing is decided from the message alone |
| Agent 2, clinical | Yes: accepts listed conditions; never ranks possible ones in an undiagnosed mode |
| Agent 2, grief | No |
| Agent 3 (writing) | Yes; in grief mode only to understand who the user may be grieving |
| Agent 4 (review) | Yes: repairs wording that implies a condition in an undiagnosed mode |

Code: `agent/backend/tools/user_profile.py`, `agent/backend/ai_wrapper/profile.py`

## Scope

Held talks only about mental health, wellbeing, caregiving and grief.

- **Declined:** general-assistant work on any other subject, such as space, science, sport, coding, math, homework, recipes, travel or shopping. Agent 1 judges by what the user wants, not by a subject list.
- **How:** a short, warm fixed reply from code, in three wordings that take turns. No answer is written, so none can slip through, and only one AI call is used.
- **Still in scope:**
  - feelings about any subject ("I'm stressed about my coding exam");
  - practical caregiving, wellbeing (sleep, food and mood), and questions about the app;
  - personal details the user asks Held to remember;
  - fear, paranoia and safety worries.
- **Never declined:**
  - a message in a chat where the user's own risk came up ("I want to die... just kidding, what's for dinner?");
  - a message that may show risk, such as a lethal-dose question;
  - a grief note sent from the app.
- **Mixed messages:** the reply answers the support part and says in one sentence that it can't help with the rest.

## Context memory

The bot remembers the current chat through its latest messages and a rolling summary. A new chat starts fresh, like ChatGPT.

| Memory layer | What is kept | Limit |
| --- | --- | --- |
| Same chat | The latest messages, word for word | 20 messages. A message over 4,000 characters keeps its start and end |
| Older part of a long chat | A rolling summary: facts, feelings and goals, advice already given, safety answers | Up to 250 words, refreshed every 10 messages in the background |
| Other chats | The user memory: a short summary used as silent background | Up to 150 words, from the latest 10 chats, rebuilt when a new chat starts |
| Earlier chat, when asked | Excerpts found by full-text search when the user refers back | Last 365 days, top 5, safety talk removed |
| Safety concerns | The user's earlier messages that show risk | 3 per chat, from the last 6 days; checked on once per account |

### How the agents use it

- **Each agent gets only what it needs.**
  - Agent 1: the summary, the user's own recent messages and the last 6 messages.
  - Agent 2: the summary and the last 10.
  - Agent 3: everything.
  - Agent 4: everything except the bot's own earlier replies.
- **Routing sees the current chat only,** so an old topic cannot turn a new message into a crisis.
- **No unprompted mentions.** A new chat never brings up earlier chats unless the user refers back.
- **Tentative memory.**
  - The bot says "I think you mentioned..." and uses a memory only when the latest message is about the same thing.
  - It never swaps in a similar detail (a dog for a cat).
- **Private memory list.** "What do you remember about me?" leaves out abuse, suicidal thoughts and self-harm, with no hint, because someone else may hold the phone.
- **Safety check-in.**
  - An earlier concern is checked on once per account, gently, only when the user talks about how they feel.
  - It is never raised in a reply about a new loss.
- **No repeated advice or questions.** The summary's "Already suggested" list and the latest messages stop repeats, and code rewrites a safety question the user already answered.

Code: `agent/backend/ai_wrapper/memory.py`, `agent/backend/db/repository.py`

## Grief notes

Grief notes are the Held app's daily journal: a date, a mood and text. The app writes them; the agent only reads them.

### Reading notes in chat (tools)

Agent 3 may call three read-only tools when the answer depends on the user's notes, for example "how has my last month been?". Gemini decides when to call them.

| Tool | What it returns |
| --- | --- |
| `recent_notes` | Notes from the last N days (at most 90 days, 20 notes) |
| `note_by_date` | The note for one date |
| `mood_summary` | How many notes, and how often each mood appeared, overall and by week |

- **Only the user's own notes:** the user comes from the token, and no tool takes a user ID or name.
- **Limits:** arguments are validated, unknown tools are rejected, each note is cut to 1,500 characters, and a message may make at most 4 calls.
- **Notes are data:** note text is never followed as instructions, and crisis language in a note takes the crisis path.
- **No scores:** the reply can describe counts and moods, but never diagnoses or claims the user is improving or getting worse.
- **Logs:** dates are UTC, and the log never holds note text.

### A note sent from the app

With `"isNote": true` the note starts its own chat, marked `isNote`, and gets a short grief companion's reply. Tools are off for that message. The mood reaches the agent only if the app writes it into the text; sending the note's ID instead is a proposed improvement.

Code: `agent/backend/grief/note_tools.py`, `agent/backend/db/grief_notes.py`

## DSM-5-TR

DSM-5-TR knowledge comes from Gemini itself, guided by the agent prompts. There is no stored copy of the book and no RAG.

- **Agent 2** writes general education in its private clinical reading.
- **Agent 3** explains conditions in everyday words, and does not name the DSM unless the user does.
- **Factual questions** get a direct answer, then the key points as bold-labelled bullets.
- **Agent 4** accepts accurate general education, and removes page numbers, invented citations and anything that applies criteria to the user.
- **The information stays general.** The bot never compares the user with criteria, counts symptoms or diagnoses. "Do I have depression?" gets a response to how they feel and a suggestion to see a clinician.

## Grief and crisis handling

Two layers decide whether a message is a crisis, and both choose crisis when unsure.

| Layer | How it decides |
| --- | --- |
| 1. Instant local check | Word patterns before any AI, in English, Spanish and Roman Urdu. First-person intent, a plan, means or harm to others is always a crisis. A story about someone else's death is not |
| 2. Agent 1 | Reads the meaning. Active intent, a plan, an overdose, someone else in danger, a request for a lethal amount (however framed), and indirect warning signs (life insurance after suicide, giving things away, goodbye letters) are crises |

### What a crisis reply does

1. **Immediate danger first.** Pills, a bridge or a weapon: the first lines are the safety action, such as "Step back from the edge and call 911".
2. **Verified lines.**
   - The reply gives lines only from `support_resources.py`: in the US, 988 for any crisis, 911, Poison Control for pills or poison, and the National Domestic Violence Hotline for abuse.
   - The first crisis reply of a chat always carries one; code adds it when the reply did not.
3. **Step-by-step risk questions.** One per reply, skipping what was already answered:
   - thoughts of ending their life;
   - a plan or method;
   - access to that means;
   - timing.
4. **Means safety.** It gently encourages distance from the means, such as giving the pills to someone else.
5. **The user's wishes.** "No hotline numbers, just talk" is respected, but the bot still checks safety and mentions 988 once.
6. **Grounding only when it fits.** It is skipped for information requests, abuse disclosures and light messages, and never comes before a safety action.
7. **Never left unanswered.** When Gemini is unavailable, a message that may show risk gets a fixed reply with 988 and 911.

### What a grief reply does

- **Agent 2** gives a companioning read: who was lost, the feelings present, the details to honour.
- **Agent 3** stays with the pain instead of fixing it, with no clinical words or timelines and no "time heals".
- **A loss by suicide:**
  - the reply says "died by suicide", never "committed";
  - it offers suicide-loss support (AFSP, Alliance of Hope) once;
  - it gently asks how the user is holding up, including their own safety.
- **Agent 4** checks that nothing treats the loss as an illness.

Code: `agent/backend/ai_wrapper/agents/agent1_intent_guardrail/policy.py`, `agent/backend/ai_wrapper/support_resources.py`, `agent/backend/ai_wrapper/agents/agent3_empathy_synthesizer/prompt.py`

## Human, message-led replies

Replies should read like a perceptive, caring person, not a support script.

| Area | Rule |
| --- | --- |
| Voice | Plain, natural language with "I" and "you" |
| Led by the message | Matches what the person wants (to vent, understand, decide, or a practical answer) in length and tone |
| No set phrases | No "I'm so sorry to hear that", "Thank you for sharing" or "I'm here for you" (crisis replies may keep a line like "I'm really glad you told me") |
| No assumptions | No gender, name or details the user did not give; ambiguous words ("I feel high") are asked about |
| Formatting | Short paragraphs of 2 to 3 sentences, bold questions, bold-labelled bullets, numbered steps |
| Honesty | Says it is an AI, not a therapist or doctor, when asked; declines to diagnose or prescribe |
| Repetition | Does not reuse its earlier sentences, openings, advice or questions |

Agent 4 works with a light touch. It repairs only these:

- diagnoses and unsupported certainty;
- medication-change instructions;
- invented facts;
- unsafe content, including lethal amounts;
- support numbers that are not on the verified list.

It keeps the warmth and the formatting.

### Natural openings

Code reads the first sentence of each draft. If it echoes the user ("It sounds like..."), sounds like an article, or (outside a crisis) starts with a set phrase, Agent 3 is asked for one rewrite. The second draft goes on either way, so this check never blocks a reply.

Code: `agent/backend/ai_wrapper/agents/agent3_empathy_synthesizer/openings.py`

## Cost and database use

Measured on 2026-11-19 with real Gemini calls, for the first message of a chat:

| Route | AI calls | Input tokens | Estimated cost |
| --- | --- | --- | --- |
| General support | 3 | 10,840 | $0.0056 |
| Clinical | 4 | 11,608 | $0.0068 |
| Grief | 4 | 11,277 | $0.0065 |
| Crisis (instant check) | 2 | 9,429 | $0.0073 |
| Off-topic | 1 | 1,416 | $0.0006 |

- **Cost stays flat.** A message costs about the same however long the chat is, because at most 20 messages are sent. A new chat adds one call for its title.
- **Database:** a message in an existing chat costs 1 read and 1 write, plus the sign-in check. The summary adds 1 small read and 1 write every 10 messages.
- **Retries:** a passing Gemini failure is tried again, up to 3 attempts. A short rate limit (10 seconds or less) is waited out once. A paid Gemini key is needed: in the developer's audit on the free tier, 13% of messages hit a rate limit.
- **Token use:** each reply's token use is stored in `ChatMessage.metadata.usage` and logged as `usage turn ...`. It is shown to the app only when `SHOW_TOKEN_USAGE=true`.

## Current limits and decisions

| Item | Status | Suggested action |
| --- | --- | --- |
| Crisis lines for other countries | US only; others get Find A Helpline | Add each launch country's verified lines; decide how the app sends the user's country |
| Clinical review | Tested by the developer's audits, not by a clinician | Have a clinician review crisis and grief replies, and the 6-day check-in limit |
| Stream errors in the app | The backend sends an error event; the app ignores it | The app shows it with Retry and keeps the user's text |
| Memory controls | No way for the user to see or delete memory | Build a "What Held remembers" screen and endpoints |
| Grief note mood | Reaches the agent only through the note's text | Let the app send the note's ID, so the agent reads date, mood and text |
| Credentials | Some were shared during testing | Rotate the database password and secrets |
| Abuse limits | No per-user rate limit | Add one before public launch |
