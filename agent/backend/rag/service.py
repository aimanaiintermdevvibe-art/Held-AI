"""Document upload (extract, chunk, store; embedding runs in rag/ingestion.py) and retrieval for chat."""
import asyncio
import hashlib
import logging
import re
import time
from pathlib import PurePath
from typing import Any

from backend.ai_wrapper.client import GeminiClient
from backend.core.config import Settings
from backend.db import documents as store
from backend.rag.chunking import chunk_text
from backend.rag.extract import DocumentError, Section, extract_sections
from backend.rag.ingestion import logger as progress_log

logger = logging.getLogger("held_ai.rag")

# A message that points at the uploaded material ("summarise my document") is too generic
# to match any passage closely, so it gets the nearest chunks even below the threshold.
_REFERS_TO_DOCUMENTS = re.compile(
    r"\b(documents?|docs?|files?|pdfs?|uploads?|uploaded|attachments?|handouts?|leaflets?)\b",
    re.IGNORECASE,
)


def _chunks(sections: list[Section], settings: Settings) -> list[dict[str, Any]]:
    return [
        {"content": text, "metadata": {"page": section.page} if section.page else {}}
        for section in sections
        for text in chunk_text(section.text, settings.document_chunk_chars, settings.document_chunk_overlap)
    ]


async def ingest_document(
    settings: Settings,
    *,
    user_id: str,
    filename: str,
    content_type: str | None,
    data: bytes,
) -> tuple[dict[str, Any], bool]:
    """Extract and chunk an uploaded file and store the chunks for background embedding
    (status "queued"; the IngestionWorker fills the embeddings). Any length is accepted
    unless DOCUMENT_MAX_CHUNKS is set.

    Returns (document, created). The same file uploaded again returns the stored document
    (created=False); if its embedding had stopped, it is queued again to resume.
    """
    filename = PurePath(filename or "document").name[:255]
    if not data:
        raise DocumentError("The uploaded file is empty.")
    if len(data) > settings.document_max_bytes:
        raise DocumentError(f"Files are limited to {settings.document_max_bytes // (1024 * 1024)} MB.", 413)

    sha256 = hashlib.sha256(data).hexdigest()
    existing = await store.find_document_by_hash(user_id, sha256)
    if existing:
        if existing["status"] == "failed":
            await store.set_document_status(existing["id"], "queued")
            existing.update(status="queued", error=None)
            progress_log.info("%s (%s): uploaded again; resuming at %s/%s chunks.", filename,
                              existing["id"][:8], existing["chunk_count"], existing["total_chunks"])
        return existing, False

    started = time.monotonic()
    # Text extraction and chunking are CPU work; keep them off the event loop.
    sections = await asyncio.to_thread(extract_sections, filename, data)
    chunks = await asyncio.to_thread(_chunks, sections, settings)
    if settings.document_max_chunks and len(chunks) > settings.document_max_chunks:
        raise DocumentError(
            f"The document is too long to index ({len(chunks)} passages; the limit is "
            f"{settings.document_max_chunks}). Split it with agent/scripts/split_document.py.",
            413,
        )

    pages = sum(1 for s in sections if s.page)
    document, created = await store.create_document(
        user_id=user_id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=sha256,
        content="\n\n".join(s.text for s in sections),
        embedding_model=settings.embedding_model,
        metadata={"pages": pages} if pages else {},
        chunks=chunks,
    )
    progress_log.info(
        "%s (%s): uploaded by %s, %.1f MB%s -> %s chunks stored in %.1fs; queued for embedding.",
        filename, document["id"][:8], user_id, len(data) / (1024 * 1024),
        f", {pages} pages" if pages else "", len(chunks), time.monotonic() - started,
    )
    return document, created


async def search_documents(
    client: GeminiClient,
    settings: Settings,
    *,
    user_id: str,
    query: str,
    top_k: int,
    document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The user's chunks most similar to the query (no threshold), most similar first."""
    [embedding] = await client.embed(
        [query],
        model=settings.embedding_model,
        task_type="RETRIEVAL_QUERY",
        dimensions=store.EMBEDDING_DIMENSIONS,
    )
    return await store.search_chunks(user_id, embedding, top_k, document_ids)


async def excerpts_for_chat(
    client: GeminiClient, settings: Settings, *, user_id: str, prompt: str,
) -> list[dict[str, Any]]:
    """Passages from the user's documents relevant to a chat message; [] when the user has no
    documents or nothing is relevant. Best effort: a failure only means no excerpts."""
    try:
        if not await store.has_documents(user_id):
            return []
        results = await search_documents(
            client, settings, user_id=user_id, query=prompt, top_k=settings.rag_top_k,
        )
    except (RuntimeError, ValueError) as exc:
        logger.warning("Document retrieval failed for user %s: %s", user_id, exc)
        return []
    relevant = [r for r in results if r["similarity"] >= settings.rag_min_similarity]
    if not relevant and _REFERS_TO_DOCUMENTS.search(prompt):
        relevant = results
    logger.info(
        "Retrieved %s of %s passages for user %s (best similarity %s).",
        len(relevant), len(results), user_id, results[0]["similarity"] if results else None,
    )
    return relevant


def as_excerpts(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retrieved chunks in the shape the agents receive (document_excerpts)."""
    excerpts = []
    for r in results:
        excerpt = {"source": r["filename"], "excerpt": r["content"], "relevance": r["similarity"]}
        if r["metadata"].get("page"):
            excerpt["page"] = r["metadata"]["page"]
        excerpts.append(excerpt)
    return excerpts


def as_sources(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retrieved chunks as response metadata, so clients can show what a reply drew on."""
    return [
        {
            "document_id": r["document_id"],
            "filename": r["filename"],
            "chunk_index": r["chunk_index"],
            "page": r["metadata"].get("page"),
            "similarity": r["similarity"],
        }
        for r in results
    ]
