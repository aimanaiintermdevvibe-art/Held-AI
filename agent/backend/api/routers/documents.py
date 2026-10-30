import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status

from backend.ai_wrapper.client import ProviderBusyError
from backend.api.routers.agentic import gemini_client, settings
from backend.core.auth import AccessTokenPayload, require_auth
from backend.db import documents as store
from backend.rag.extract import DocumentError
from backend.rag.ingestion import IngestionWorker
from backend.rag.service import ingest_document, search_documents
from backend.schemas.documents import DocumentSearchRequest

logger = logging.getLogger("held_ai.api.documents")

# Documents belong to the signed-in user: each user uploads, lists, searches and deletes
# only their own, and chat replies draw only on the caller's documents.
router = APIRouter(prefix="/agentic/documents", tags=["Documents"], dependencies=[Depends(require_auth)])
# Embeds uploaded documents in the background; started on first use, resumed at startup.
ingestion_worker = IngestionWorker(gemini_client, settings)


def _provider_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProviderBusyError):
        wait = exc.retry_after or 60
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"The AI is getting too many requests right now. Please try again in about {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Document service is unavailable.")


@router.post("", status_code=status.HTTP_202_ACCEPTED, summary="Upload a document for retrieval")
async def upload_document(
    request: Request, response: Response, file: UploadFile = File(...),
) -> Dict[str, Any]:
    """Extract and chunk a .txt, .md, .pdf or .docx file (multipart field `file`) and queue
    its chunks for embedding in the background. Any length is accepted.

    Returns 202 with status "queued" for a new document; follow progress with
    GET /agentic/documents/{id} (status, chunk_count / total_chunks, progress) or in
    logs/document_ingestion.log. The same file uploaded again returns the stored document
    with 200 (and resumes it if its embedding had stopped).
    """
    principal: AccessTokenPayload = request.state.user
    data = await file.read(settings.document_max_bytes + 1)
    try:
        document, created = await ingest_document(
            settings, user_id=principal["user_id"], filename=file.filename or "document",
            content_type=file.content_type, data=data,
        )
    except DocumentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("Document upload failed: %s", exc)
        raise _provider_error(exc) from exc
    if document["status"] in ("queued", "processing"):
        ingestion_worker.enqueue(document["id"])
    if not created:
        response.status_code = status.HTTP_200_OK
    return {"document": document, "created": created}


@router.get("", summary="List your documents")
async def list_documents(request: Request) -> List[Dict[str, Any]]:
    principal: AccessTokenPayload = request.state.user
    try:
        return await store.list_documents(principal["user_id"])
    except RuntimeError as exc:
        raise _provider_error(exc) from exc


@router.post("/search", summary="Retrieve the passages most relevant to a query")
async def search(payload: DocumentSearchRequest, request: Request) -> Dict[str, Any]:
    """Nearest chunks by cosine similarity, most similar first (chat replies only use those
    above RAG_MIN_SIMILARITY)."""
    principal: AccessTokenPayload = request.state.user
    try:
        results = await search_documents(
            gemini_client, settings, user_id=principal["user_id"], query=payload.query,
            top_k=payload.top_k, document_ids=payload.document_ids,
        )
    except RuntimeError as exc:
        raise _provider_error(exc) from exc
    return {"query": payload.query, "min_similarity_for_chat": settings.rag_min_similarity, "results": results}


@router.get("/{document_id}", summary="Get a document's status and a page of its chunks")
async def get_document(
    document_id: str,
    request: Request,
    chunk_offset: int = Query(default=0, ge=0),
    chunk_limit: int = Query(default=50, ge=0, le=500),
) -> Dict[str, Any]:
    """status is queued, processing, ready or failed; chunk_count of total_chunks are embedded."""
    principal: AccessTokenPayload = request.state.user
    try:
        document = await store.get_document(document_id, principal["user_id"], chunk_offset, chunk_limit)
    except RuntimeError as exc:
        raise _provider_error(exc) from exc
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return document


@router.delete("/{document_id}", summary="Delete a document and its chunks")
async def delete_document(document_id: str, request: Request) -> Dict[str, bool]:
    principal: AccessTokenPayload = request.state.user
    try:
        deleted = await store.delete_document(document_id, principal["user_id"])
    except RuntimeError as exc:
        raise _provider_error(exc) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return {"success": True}
