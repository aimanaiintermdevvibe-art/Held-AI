"""Live end-to-end check of document upload, retrieval and RAG chat against a running agent.

    python agent/scripts/rag_smoke_test.py [--user 1] [--agent http://127.0.0.1:8000] [--log logs/rag.log]

It signs in the test account (users/user<N>.json), uploads a generated Word document,
checks duplicate handling, runs searches, asks chat questions about the document and an
unrelated one, then deletes the document and the chats it created. Every step is written
to the log file. No tokens or passwords are logged.
"""
import argparse
import io
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))

from backend.ai_wrapper.profile import describe, user_context  # noqa: E402
from backend.core.held_client import HeldAuthClient  # noqa: E402
from backend.core.user_accounts import UserAccountFile, user_file_for  # noqa: E402

FACTS = [
    ("When does the memory café meet, and where?", ["thursday", "15:00", "garden room"]),
    ("What are the visiting hours?", ["10:00", "19:30"]),
    ("Who should I call about my mother's medication?", ["ward manager", "0161 555 0142"]),
]
UNRELATED = "How do I boil an egg so the yolk stays soft?"


def make_document(marker: str) -> bytes:
    """A fictional care-home guide; the marker makes each run's file unique."""
    import docx

    document = docx.Document()
    document.add_heading("Willow House Care Home: Family Visitor Guide", level=1)
    paragraphs = [
        f"Reference {marker}. This guide explains how families can visit and stay involved in the care of a resident.",
        "Visiting hours are 10:00 to 19:30 every day, including weekends and bank holidays. Please sign in at "
        "reception and wash your hands at the station by the front door.",
        "Quiet time is 13:00 to 14:00, when residents rest after lunch. Visits can still happen in the lounge.",
        "The memory café meets every Thursday at 15:00 in the Garden Room. It is a relaxed hour of music, old "
        "photographs and tea for residents living with dementia and the people who love them. Families are welcome.",
        "Medication is managed only by the nursing team. Please do not bring medicines from home. For questions "
        "about a resident's medication, speak to the ward manager, Priya Shah, on 0161 555 0142 (weekdays 09:00 to 17:00).",
        "Meals are served at 08:00, 12:30 and 17:30. Families may join a resident for lunch if they let the kitchen "
        "know by 10:00 the same day.",
        "Laundry is collected every Monday and Wednesday. Please label clothing with the resident's name.",
        "Parking is free in the rear car park. The two spaces nearest the entrance are kept for blue badge holders.",
    ]
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    table = document.add_table(rows=3, cols=2)
    for row, (day, activity) in enumerate([("Tuesday", "Chair yoga at 11:00"), ("Friday", "Film afternoon at 14:30"), ("Sunday", "Church service at 10:30")]):
        table.cell(row, 0).text, table.cell(row, 1).text = day, activity
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default="1", help="HELD_ACTIVE_USER value (default 1)")
    parser.add_argument("--agent", default="http://127.0.0.1:8000", help="agent API base URL")
    parser.add_argument("--log", default=str(ROOT / "logs" / "rag_smoke_test.log"), help="log file (appended)")
    args = parser.parse_args()

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("rag_smoke_test")
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(formatter)
        log.addHandler(handler)

    failures: list[str] = []

    def check(condition: bool, description: str) -> None:
        log.info("%s %s", "PASS" if condition else "FAIL", description)
        if not condition:
            failures.append(description)

    log.info("=" * 78)
    log.info("RAG smoke test started %s | agent %s | user %s", datetime.now(timezone.utc).isoformat(timespec="seconds"), args.agent, args.user)

    # 1. Sign in the test account (saved token -> refresh -> password login).
    account = UserAccountFile(user_file_for(args.user))
    held = HeldAuthClient(account)
    profile = held.sign_in()
    account.remember_profile(profile)
    log.info("Step 1  signed in as %s (%s) | mode: %s", profile.get("fullName"), profile.get("email"), describe(user_context(profile)))

    def headers() -> dict:
        return {"Authorization": f"Bearer {held.get_valid_token()}"}

    api = httpx.Client(base_url=args.agent, timeout=300)
    created_sessions: list[str] = []

    def chat(prompt: str) -> httpx.Response:
        """POST /agentic/run, waiting out Gemini's per-minute limit (free tier: 15 requests)
        and retrying once after a transient provider failure (503)."""
        retried_503 = False
        for _ in range(5):
            res = api.post("/agentic/run", headers=headers(), json={"prompt": prompt})
            if res.status_code == 429:
                wait = int(res.headers.get("Retry-After", "30")) + 5
                log.info("        HTTP 429: rate limited by Gemini; waiting %ss and retrying", wait)
                time.sleep(wait)
            elif res.status_code == 503 and not retried_503:
                retried_503 = True
                log.info("        HTTP 503: %s; retrying once", res.json().get("detail"))
                time.sleep(5)
            else:
                return res
        return res
    document_id = None
    try:
        # 2. Upload a new, uniquely marked document.
        marker = uuid.uuid4().hex[:8]
        filename = f"willow-house-visitor-guide-{marker}.docx"
        data = make_document(marker)
        res = api.post("/agentic/documents", headers=headers(), files={"file": (filename, data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        body = res.json()
        check(res.status_code == 202, f"Step 2  upload {filename} ({len(data)} bytes) -> HTTP {res.status_code} (accepted)")
        document = body.get("document", {})
        document_id = document.get("id")
        log.info("        document id %s | status %s | %s chunks to embed | model %s", document_id,
                 document.get("status"), document.get("total_chunks"), document.get("embedding_model"))
        # Embedding runs in the background; wait for it as a client would.
        deadline = time.monotonic() + 600
        while document_id and time.monotonic() < deadline:
            document = api.get(f"/agentic/documents/{document_id}?chunk_limit=0", headers=headers()).json()
            if document.get("status") in ("ready", "failed"):
                break
            time.sleep(2)
        check(document.get("status") == "ready",
              f"        background embedding -> {document.get('status')} "
              f"({document.get('chunk_count')}/{document.get('total_chunks')} chunks)")

        # 3. Uploading the same file again returns the stored document.
        res = api.post("/agentic/documents", headers=headers(), files={"file": (filename, data, "application/octet-stream")})
        check(res.status_code == 200 and res.json().get("created") is False and res.json()["document"]["id"] == document_id,
              f"Step 3  duplicate upload -> HTTP {res.status_code}, created={res.json().get('created')}, same id")

        # 4. The document is listed with its chunks.
        listed = api.get("/agentic/documents", headers=headers()).json()
        check(any(d["id"] == document_id for d in listed), f"Step 4  listed among {len(listed)} document(s)")
        detail = api.get(f"/agentic/documents/{document_id}", headers=headers()).json()
        for chunk in detail.get("chunks", []):
            log.info("        chunk %s (%s chars): %s...", chunk["chunk_index"], len(chunk["content"]), chunk["content"][:90].replace("\n", " "))

        # 5. Retrieval: the right passage ranks first; an unrelated query scores low.
        for question, expected in FACTS + [(UNRELATED, None)]:
            res = api.post("/agentic/documents/search", headers=headers(), json={"query": question, "top_k": 3})
            results = res.json().get("results", [])
            threshold = res.json().get("min_similarity_for_chat")
            scores = ", ".join(f"#{r['chunk_index']}={r['similarity']}" for r in results)
            log.info("Step 5  search %r -> %s (chat threshold %s)", question, scores, threshold)
            used = [r for r in results if r["similarity"] >= threshold]  # what a chat reply would be given
            if expected:
                text = " ".join(r["content"].lower() for r in used)
                check(bool(used) and all(term in text for term in expected),
                      f"        passages above the threshold ({len(used)}) contain {expected}")
            else:
                check(not used, "        no passage clears the threshold")

        # 6. Chat: answers come from the document and report their sources.
        for question, expected in FACTS:
            res = chat(question)
            body = res.json()
            if body.get("session_id"):
                created_sessions.append(body["session_id"])
            answer = body.get("response", "")
            sources = body.get("sources") or []
            log.info("Step 6  chat %r -> HTTP %s | sources: %s", question, res.status_code,
                     [(s["filename"][-13:], s["chunk_index"], s["similarity"]) for s in sources])
            log.info("        answer: %s", answer.replace("\n", " ")[:600])
            check(res.status_code == 200 and bool(sources), "        reply was given passages from the document")
            check(all(term in answer.lower() for term in expected), f"        answer states {expected}")

        res = chat(UNRELATED)
        body = res.json()
        if body.get("session_id"):
            created_sessions.append(body["session_id"])
        log.info("Step 6  chat %r -> HTTP %s | sources: %s", UNRELATED, res.status_code, body.get("sources"))
        log.info("        answer: %s", body.get("response", "").replace("\n", " ")[:300])
        check(res.status_code == 200 and not body.get("sources"), "        unrelated question used no document passages")
    finally:
        # 7. Clean up: the document (chunks cascade) and the chats created by this test.
        if document_id:
            res = api.delete(f"/agentic/documents/{document_id}", headers=headers())
            gone = api.get(f"/agentic/documents/{document_id}", headers=headers()).status_code == 404
            check(res.status_code == 200 and gone, f"Step 7  deleted document {document_id} (now 404: {gone})")
        for session_id in created_sessions:
            api.delete(f"/agentic/sessions/{session_id}", headers=headers())
        log.info("Step 7  deleted %s test chat session(s)", len(created_sessions))

    log.info("RESULT  %s (%s failure(s))", "PASSED" if not failures else "FAILED", len(failures))
    for failure in failures:
        log.info("        failed: %s", failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
