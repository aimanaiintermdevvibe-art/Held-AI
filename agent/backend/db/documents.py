import json
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

from backend.db.connection import get_db_pool

logger = logging.getLogger("held_ai.documents")

# ==============================================================================
# Uploaded documents and their embedded chunks (migrations 002 and 004, pgvector).
#
# An upload stores the document and all its chunks at once, with empty embeddings
# (status "queued"). The ingestion worker fills the embeddings batch by batch
# ("processing"), then marks the document "ready" (or "failed", resumable). Only
# embedded chunks are searched. Every query is scoped to the owning user.
# ==============================================================================

EMBEDDING_DIMENSIONS = 768  # must match document_chunks.embedding vector(768)
UNFINISHED = ("queued", "processing", "paused")

DOCUMENT_COLUMNS = """
    d.id, d.user_id, d.filename, d.content_type, d.size_bytes, d.sha256, d.status,
    d.chunk_count, d.total_chunks, d.error, d.embedding_model, d.metadata, d.created_at, d.updated_at
"""


def _uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value).strip())
    except (ValueError, AttributeError):
        return None


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _vector(values: List[float]) -> str:
    """pgvector's text form, e.g. [0.1,-0.2]; cast with ::extensions.vector in SQL."""
    return "[" + ",".join(format(float(v), ".8g") for v in values) + "]"


def _document_dict(row: Any) -> Dict[str, Any]:
    total = row["total_chunks"]
    return {
        "id": str(row["id"]),
        "user_id": row["user_id"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "status": row["status"],
        "chunk_count": row["chunk_count"],  # chunks embedded so far
        "total_chunks": total,
        "progress": round(100 * row["chunk_count"] / total, 1) if total else (100.0 if row["status"] == "ready" else 0.0),
        "error": row["error"],
        "embedding_model": row["embedding_model"],
        "metadata": _json(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def _pool() -> asyncpg.Pool:
    pool = await get_db_pool()
    if not pool:
        raise RuntimeError("Database is unavailable.")
    return pool


async def find_document_by_hash(user_id: str, sha256: str) -> Optional[Dict[str, Any]]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {DOCUMENT_COLUMNS} FROM public.documents d WHERE d.user_id = $1 AND d.sha256 = $2;",
            user_id, sha256,
        )
        return _document_dict(row) if row else None


async def create_document(
    *,
    user_id: str,
    filename: str,
    content_type: Optional[str],
    size_bytes: int,
    sha256: str,
    content: str,
    embedding_model: str,
    metadata: Dict[str, Any],
    chunks: List[Dict[str, Any]],
) -> tuple[Dict[str, Any], bool]:
    """Store a document and all its chunks (without embeddings) in one transaction.

    chunks are {"content", "metadata"} in order. The document starts "queued" for the
    ingestion worker. Returns (document, created); created is False when the same file
    was already stored for this user.
    """
    pool = await _pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO public.documents AS d (
                        user_id, filename, content_type, size_bytes, sha256, content,
                        status, chunk_count, total_chunks, embedding_model, metadata
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, 'queued', 0, $7, $8, $9::jsonb)
                    RETURNING {DOCUMENT_COLUMNS};
                    """,
                    user_id, filename, content_type, size_bytes, sha256, content,
                    len(chunks), embedding_model, json.dumps(metadata),
                )
                await conn.copy_records_to_table(
                    "document_chunks",
                    schema_name="public",
                    columns=["document_id", "user_id", "chunk_index", "content", "metadata"],
                    records=[
                        (row["id"], user_id, index, chunk["content"], json.dumps(chunk["metadata"]))
                        for index, chunk in enumerate(chunks)
                    ],
                )
        except asyncpg.UniqueViolationError:
            # The same file was uploaded concurrently; keep the copy that won.
            existing = await find_document_by_hash(user_id, sha256)
            if existing:
                return existing, False
            raise
        logger.info("Stored document %s (%s chunks) for user %s.", row["id"], len(chunks), user_id)
        return _document_dict(row), True


async def list_documents(user_id: str) -> List[Dict[str, Any]]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {DOCUMENT_COLUMNS} FROM public.documents d WHERE d.user_id = $1 ORDER BY d.created_at DESC;",
            user_id,
        )
        return [_document_dict(r) for r in rows]


async def get_document(
    document_id: str, user_id: Optional[str] = None, chunk_offset: int = 0, chunk_limit: int = 50,
) -> Optional[Dict[str, Any]]:
    """The document with a page of its chunks (text and metadata, without embeddings).

    user_id=None is for the ingestion worker only; API callers always pass the owner.
    """
    key = _uuid(document_id)
    if key is None:
        return None
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {DOCUMENT_COLUMNS} FROM public.documents d "
            "WHERE d.id = $1 AND ($2::text IS NULL OR d.user_id = $2);",
            key, user_id,
        )
        if not row:
            return None
        chunks = await conn.fetch(
            "SELECT chunk_index, content, metadata, embedding IS NOT NULL AS embedded "
            "FROM public.document_chunks WHERE document_id = $1 ORDER BY chunk_index OFFSET $2 LIMIT $3;",
            key, chunk_offset, chunk_limit,
        )
        document = _document_dict(row)
        document["chunks"] = [
            {"chunk_index": c["chunk_index"], "content": c["content"], "metadata": _json(c["metadata"]),
             "embedded": c["embedded"]}
            for c in chunks
        ]
        return document


async def delete_document(document_id: str, user_id: str) -> bool:
    """Delete a document and (by cascade) its chunks."""
    key = _uuid(document_id)
    if key is None:
        return False
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM public.documents WHERE id = $1 AND user_id = $2;", key, user_id,
        )
        return result.endswith(" 1")


async def has_documents(user_id: str) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM public.document_chunks WHERE user_id = $1 AND embedding IS NOT NULL);",
            user_id,
        ))


# -- ingestion worker -------------------------------------------------------------

async def unfinished_document_ids() -> List[str]:
    """Documents still queued, processing or paused (oldest first), e.g. after a restart."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM public.documents WHERE status = ANY($1::text[]) ORDER BY created_at;",
            list(UNFINISHED),
        )
        return [str(r["id"]) for r in rows]


async def set_document_status(document_id: str, status: str, error: Optional[str] = None) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.documents SET status = $2, error = $3, updated_at = now() WHERE id = $1;",
            _uuid(document_id), status, error,
        )


async def next_unembedded_chunks(document_id: str, limit: int) -> List[Dict[str, Any]]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, chunk_index, content FROM public.document_chunks "
            "WHERE document_id = $1 AND embedding IS NULL ORDER BY chunk_index LIMIT $2;",
            _uuid(document_id), limit,
        )
        return [{"id": r["id"], "chunk_index": r["chunk_index"], "content": r["content"]} for r in rows]


async def save_chunk_embeddings(document_id: str, embeddings: List[tuple[Any, List[float]]]) -> int:
    """Store (chunk id, embedding) pairs and return how many of the document's chunks are embedded."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                "UPDATE public.document_chunks SET embedding = $2::extensions.vector WHERE id = $1;",
                [(chunk_id, _vector(vector)) for chunk_id, vector in embeddings],
            )
            return await conn.fetchval(
                """
                UPDATE public.documents d
                SET chunk_count = (SELECT count(*) FROM public.document_chunks c
                                   WHERE c.document_id = d.id AND c.embedding IS NOT NULL),
                    updated_at = now()
                WHERE d.id = $1
                RETURNING d.chunk_count;
                """,
                _uuid(document_id),
            )


async def search_chunks(
    user_id: str,
    embedding: List[float],
    limit: int,
    document_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """The user's embedded chunks nearest to `embedding` by cosine similarity, most similar first."""
    keys = [k for k in (_uuid(d) for d in document_ids or []) if k is not None] if document_ids else None
    if document_ids and not keys:
        return []
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Keep scanning the HNSW index until `limit` rows pass the owner filter.
            await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order;")
            rows = await conn.fetch(
                """
                SELECT c.document_id, d.filename, c.chunk_index, c.content, c.metadata,
                       1 - (c.embedding <=> $2::extensions.vector) AS similarity
                FROM public.document_chunks c
                JOIN public.documents d ON d.id = c.document_id
                WHERE c.user_id = $1 AND c.embedding IS NOT NULL
                  AND ($4::uuid[] IS NULL OR c.document_id = ANY($4::uuid[]))
                ORDER BY c.embedding <=> $2::extensions.vector
                LIMIT $3;
                """,
                user_id, _vector(embedding), limit, keys,
            )
    results = [
        {
            "document_id": str(r["document_id"]),
            "filename": r["filename"],
            "chunk_index": r["chunk_index"],
            "metadata": _json(r["metadata"]),
            "content": r["content"],
            "similarity": round(float(r["similarity"]), 4),
        }
        for r in rows
    ]
    # relaxed_order may return near-ties slightly out of order.
    return sorted(results, key=lambda r: r["similarity"], reverse=True)
