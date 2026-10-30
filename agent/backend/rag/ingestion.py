"""Background embedding of uploaded documents, one document at a time, with progress logs.

An upload stores every chunk straight away; this worker then embeds them in batches
(EMBEDDING_BATCH_SIZE chunks per Gemini request), saving each batch as it goes, so a
document of any length is indexed without holding an HTTP request open. Per-minute rate
limits are waited out; when the daily quota is used up (Gemini free tier: 1,000 chunks a
day) the document is paused and continues by itself when the quota resets. Transient
failures are retried, and unfinished or paused documents are picked up again when the
server starts. Progress goes to the console and to logs/document_ingestion.log.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from backend.ai_wrapper.client import GeminiClient, ProviderBusyError
from backend.core.config import Settings
from backend.db import documents as store

LOG_FILE = Path(__file__).resolve().parents[3] / "logs" / "document_ingestion.log"
MAX_RATE_LIMIT_WAIT_SECONDS = 300  # a longer Retry-After means the daily quota is used up
MAX_RATE_LIMIT_WAITS = 30          # consecutive per-minute waits before pausing instead
PAUSE_SECONDS = 600                # shortest pause when the quota is used up
MAX_FAILURES = 3                   # consecutive other failures before giving up for now

logger = logging.getLogger("held_ai.ingestion")


def _configure_logging() -> None:
    """Progress lines on the server console and in logs/document_ingestion.log."""
    if logger.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s [ingestion] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("Cannot write %s: %s", LOG_FILE, exc)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_configure_logging()


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m" if seconds >= 3600 else f"{seconds // 60}m{seconds % 60:02d}s"


class IngestionWorker:
    def __init__(self, client: GeminiClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._queued: set[str] = set()

    def enqueue(self, document_id: str) -> None:
        """Schedule a document; a document already waiting is not queued twice."""
        if not self.settings.ingestion_worker:
            logger.info("Document %s queued; INGESTION_WORKER is off here, so another instance embeds it.", document_id)
            return
        queue = self._ensure_running()
        if document_id not in self._queued:
            self._queued.add(document_id)
            queue.put_nowait(document_id)

    async def resume_unfinished(self) -> None:
        """Queue documents left queued or processing, e.g. by a restart. Best effort."""
        if not self.settings.ingestion_worker:
            logger.info("INGESTION_WORKER is off: this instance does not embed documents.")
            return
        try:
            ids = await store.unfinished_document_ids()
        except RuntimeError as exc:
            logger.warning("Could not look for unfinished documents: %s", exc)
            return
        for document_id in ids:
            self.enqueue(document_id)
        if ids:
            logger.info("Resuming %s unfinished document(s).", len(ids))

    async def wait_until_idle(self) -> None:
        if self._queue is not None:
            await self._queue.join()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = self._queue = None
        self._queued.clear()

    def _ensure_running(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        if self._queue is None or self._task is None or self._task.done() or self._task.get_loop() is not loop:
            self._queue = asyncio.Queue()
            self._queued.clear()
            self._task = loop.create_task(self._run(self._queue), name="document_ingestion")
        return self._queue

    async def _run(self, queue: asyncio.Queue) -> None:
        while True:
            document_id = await queue.get()
            try:
                await self.process(document_id)
            except Exception as exc:  # never let one document stop the worker
                logger.exception("Unexpected error while indexing document %s: %s", document_id, exc)
            finally:
                self._queued.discard(document_id)
                queue.task_done()

    async def process(self, document_id: str) -> str:
        """Embed every remaining chunk of a document; returns its final status."""
        document = await store.get_document(document_id, chunk_limit=0)
        if not document:
            logger.info("Document %s no longer exists; skipped.", document_id)
            return "missing"
        name = f"{document['filename']} ({document_id[:8]})"
        total = document["total_chunks"] or 0
        done = document["chunk_count"]
        if done >= total:
            await store.set_document_status(document_id, "ready")
            logger.info("%s: already complete (%s chunks).", name, total)
            return "ready"

        await store.set_document_status(document_id, "processing")
        logger.info("%s: embedding %s chunks%s, %s per request.", name, total - done,
                    f" (resuming at {done}/{total})" if done else "", self.settings.embedding_batch_size)
        started, embedded_now = time.monotonic(), 0
        rate_limit_waits = failures = 0

        while True:
            batch = await store.next_unembedded_chunks(document_id, self.settings.embedding_batch_size)
            if not batch:
                break
            try:
                vectors = await self.client.embed(
                    [c["content"] for c in batch],
                    model=self.settings.embedding_model,
                    task_type="RETRIEVAL_DOCUMENT",
                    dimensions=store.EMBEDDING_DIMENSIONS,
                )
            except ProviderBusyError as exc:
                wait = (exc.retry_after or 30) + 2
                rate_limit_waits += 1
                if wait > MAX_RATE_LIMIT_WAIT_SECONDS or rate_limit_waits > MAX_RATE_LIMIT_WAITS:
                    # The daily quota (free tier: 1,000 chunks a day) is used up: pause until it resets.
                    return await self._pause(document_id, name, done, total, max(wait, PAUSE_SECONDS))
                logger.info("%s: rate limited by Gemini at %s/%s; waiting %ss.", name, done, total, wait)
                await asyncio.sleep(wait)
                continue
            except RuntimeError as exc:
                failures += 1
                if failures > MAX_FAILURES:
                    return await self._fail(document_id, name, done, total, str(exc))
                logger.warning("%s: embedding failed (%s); retry %s/%s in %ss.",
                               name, exc, failures, MAX_FAILURES, 5 * failures)
                await asyncio.sleep(5 * failures)
                continue

            rate_limit_waits = failures = 0
            done = await store.save_chunk_embeddings(document_id, [(c["id"], v) for c, v in zip(batch, vectors)])
            embedded_now += len(batch)
            elapsed = time.monotonic() - started
            rate = embedded_now / elapsed if elapsed else 0.0
            eta = (total - done) / rate if rate else 0.0
            logger.info("%s: %s/%s chunks embedded (%.1f%%) | %.1f chunks/s | about %s left.",
                        name, done, total, 100 * done / total, rate, _duration(eta))

        await store.set_document_status(document_id, "ready")
        logger.info("%s: ready, %s chunks embedded in %s.", name, total, _duration(time.monotonic() - started))
        return "ready"

    async def _pause(self, document_id: str, name: str, done: int, total: int, seconds: float) -> str:
        """Gemini's quota is used up: mark the document paused and continue it when the quota resets
        (startup also resumes paused documents, so a restart in between loses nothing)."""
        resume_at = datetime.now() + timedelta(seconds=seconds)
        message = (
            f"Gemini's embedding quota is used up ({done}/{total} chunks embedded). Embedding continues "
            f"automatically at about {resume_at:%Y-%m-%d %H:%M} (server time) while the server is running, "
            "or when it next starts."
        )
        await store.set_document_status(document_id, "paused", message)
        asyncio.get_running_loop().call_later(seconds, self.enqueue, document_id)
        logger.warning("%s: paused at %s/%s; Gemini's embedding quota is used up. Continuing at about %s.",
                       name, done, total, f"{resume_at:%Y-%m-%d %H:%M}")
        return "paused"

    async def _fail(self, document_id: str, name: str, done: int, total: int, reason: str) -> str:
        message = f"{reason}. {done}/{total} chunks were embedded; upload the same file again to resume."
        await store.set_document_status(document_id, "failed", message)
        logger.error("%s: stopped at %s/%s: %s", name, done, total, reason)
        return "failed"
