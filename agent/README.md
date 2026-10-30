# Backend

See [setup, API contract and validation](../README.md).

- `backend/main.py`: application lifecycle; mounts the authenticated generation and session routes.
- `backend/core/auth.py`: verifies the agent API's Held HS256 bearer tokens and fetches the caller's Held profile (`GET /auth/me`) with the caller's own token.
- `backend/core/held_client.py`: Held API client for the terminal client: automatic login, token refresh with login fallback, and the user's profile.
- `backend/core/user_accounts.py`: the local test accounts (`users/user<N>.json`, chosen by `HELD_ACTIVE_USER`), each holding credentials and its own token pair, updated under a cross-process lock.
- `backend/ai_wrapper/profile.py`: turns the Held profile into one of the four user modes (myself/caregiver × diagnosed/undiagnosed).
- `backend/api/routers/agentic.py`: request execution, owned-session persistence, deadline and error handling.
- `backend/rag/`: uploaded-document text extraction (`extract.py`), chunking (`chunking.py`), and ingestion and chat retrieval (`service.py`).
- `backend/api/routers/documents.py`: upload, list, get, delete and search endpoints for documents.
- `backend/db/documents.py`: `documents` / `document_chunks` storage and owner-scoped pgvector search.
- `backend/db/repository.py`: reads and writes only `chat_sessions` and `chat_messages`; user lookups are read-only.
- `backend/db/connection.py`: async PostgreSQL pool with TLS-preferred negotiation for the Held LAN database.
- `backend/ai_wrapper/`: Gemini adapter, supervisor and four specialist agents.
- `backend/core/config.py`: provider, database and JWT settings.
- `backend/schemas/agentic.py`: prompt/response contract.
