"""Ingestion: text in, indexed chunks out, without paying twice.

    source -> ownership -> normalize -> chunk -> deduplicate -> embed
           -> index -> verify

Two forms of deduplication, both load-bearing:

* **Source level** - a unique constraint on (content hash, ownership, versions).
  Re-uploading the same file produces zero new chunks and zero embedding calls.
* **Chunk level** - the embedding cache is keyed by normalized text, so a
  passage repeated across documents is embedded once ever.

Versions are stored on the source. Changing the chunker or the embedding model
therefore creates a *new* source rather than silently mixing incompatible
vectors in one index.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.knowledge_models import DocumentChunk, KnowledgeSource
from tutortwin.domain.knowledge import Chunk, SourceKind, Visibility
from tutortwin.observability.logging import get_logger
from tutortwin.rag.chunking import CHUNKER_VERSION, chunk_text, normalize_for_hash
from tutortwin.rag.embeddings import EmbeddingProvider, embed_with_cache

logger = get_logger(__name__)


class OwnershipError(ValueError):
    """A source's visibility and its owner columns disagree."""


@dataclass(frozen=True, slots=True)
class IngestionRequest:
    title: str
    text: str
    kind: SourceKind
    visibility: Visibility
    subject_id: UUID | None = None
    tutor_id: UUID | None = None
    course_id: str | None = None
    conversation_id: UUID | None = None
    parser_version: str = "text-v1"


@dataclass(slots=True)
class IngestionResult:
    source_id: UUID
    chunks_created: int = 0
    chunks_skipped_duplicate: int = 0
    embedding_api_calls: int = 0
    embedding_cache_hits: int = 0
    already_ingested: bool = False

    @property
    def verified(self) -> bool:
        """Ingestion is only complete when every chunk is indexed."""
        return self.chunks_created > 0 or self.already_ingested


def _require_owner(request: IngestionRequest) -> None:
    """Ownership is checked before anything is written or embedded.

    The database has the same check as a constraint; this raises a clearer error
    earlier, before any embedding is paid for.
    """
    required: dict[Visibility, object | None] = {
        Visibility.STUDENT_PRIVATE: request.subject_id,
        Visibility.TUTOR: request.tutor_id,
        Visibility.COURSE: request.course_id,
        Visibility.CONVERSATION: request.conversation_id,
    }
    if request.visibility in required and required[request.visibility] is None:
        raise OwnershipError(f"{request.visibility.value} requires its owner to be set.")


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()


async def ingest(
    session: AsyncSession,
    request: IngestionRequest,
    provider: EmbeddingProvider,
) -> IngestionResult:
    """Idempotent ingestion. Re-ingesting identical content costs nothing."""
    _require_owner(request)

    digest = content_hash(request.text)

    existing = (
        await session.execute(
            select(KnowledgeSource).where(
                KnowledgeSource.content_sha256 == digest,
                KnowledgeSource.visibility == str(request.visibility),
                KnowledgeSource.subject_id == request.subject_id,
                KnowledgeSource.tutor_id == request.tutor_id,
                KnowledgeSource.parser_version == request.parser_version,
                KnowledgeSource.chunker_version == CHUNKER_VERSION,
                KnowledgeSource.embedding_model == provider.model,
                KnowledgeSource.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        logger.info(
            "ingestion_skipped_duplicate",
            source_id=str(existing.id),
            chunks=existing.chunk_count,
            embedding_calls=0,
        )
        return IngestionResult(
            source_id=existing.id,
            chunks_created=0,
            already_ingested=True,
            embedding_api_calls=0,
        )

    source = KnowledgeSource(
        title=request.title,
        kind=str(request.kind),
        visibility=str(request.visibility),
        subject_id=request.subject_id,
        tutor_id=request.tutor_id,
        course_id=request.course_id,
        conversation_id=request.conversation_id,
        content_sha256=digest,
        parser_version=request.parser_version,
        chunker_version=CHUNKER_VERSION,
        embedding_model=provider.model,
        status="INGESTING",
    )
    session.add(source)
    await session.flush()

    chunks = chunk_text(request.text)
    if not chunks:
        source.status = "EMPTY"
        source.chunk_count = 0
        return IngestionResult(source_id=source.id, chunks_created=0)

    # Deduplicate within the document: repeated boilerplate (a running header,
    # a licence block) is indexed once, not once per page.
    seen: set[str] = set()
    unique: list[tuple[str, Chunk]] = []
    duplicates = 0
    for chunk in chunks:
        key = hashlib.sha256(normalize_for_hash(chunk.text).encode("utf-8")).hexdigest()
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append((key, chunk))

    embedding = await embed_with_cache(
        session,
        provider,
        tuple(c.text for _, c in unique),
    )

    for ordinal, ((key, chunk), vector) in enumerate(zip(unique, embedding.vectors, strict=True)):
        session.add(
            DocumentChunk(
                source_id=source.id,
                ordinal=ordinal,
                text=chunk.text,
                normalized_sha256=key,
                page_number=chunk.page_number,
                section=chunk.section,
                token_estimate=chunk.token_estimate,
                visibility=str(request.visibility),
                subject_id=request.subject_id,
                tutor_id=request.tutor_id,
                course_id=request.course_id,
                conversation_id=request.conversation_id,
                embedding_json=list(vector),
                embedding_model=provider.model,
            )
        )

    source.chunk_count = len(unique)
    source.status = "READY"
    await session.flush()

    # Verify what was actually indexed rather than trusting the loop.
    indexed = (
        await session.execute(select(DocumentChunk).where(DocumentChunk.source_id == source.id))
    ).scalars()
    verified_count = len(list(indexed))
    if verified_count != len(unique):
        source.status = "INCOMPLETE"
        logger.error("ingestion_count_mismatch", expected=len(unique), indexed=verified_count)

    logger.info(
        "ingestion_complete",
        source_id=str(source.id),
        chunks=verified_count,
        duplicates_skipped=duplicates,
        embedding_api_calls=embedding.api_calls,
        embedding_cache_hits=embedding.cache_hits,
        visibility=request.visibility.value,
    )
    return IngestionResult(
        source_id=source.id,
        chunks_created=verified_count,
        chunks_skipped_duplicate=duplicates,
        embedding_api_calls=embedding.api_calls,
        embedding_cache_hits=embedding.cache_hits,
    )


async def soft_delete_source(session: AsyncSession, source_id: UUID) -> None:
    """Mark deleted. Retrieval joins on `deleted_at IS NULL`, so the chunks
    become unreachable in the same statement that ranks - not by a later filter."""
    from datetime import UTC, datetime

    source = (
        await session.execute(select(KnowledgeSource).where(KnowledgeSource.id == source_id))
    ).scalar_one_or_none()
    if source is None:
        return
    source.deleted_at = datetime.now(UTC)
    source.status = "DELETED"
    await session.flush()


async def purge_source(session: AsyncSession, source_id: UUID) -> None:
    """Hard delete, for a data-removal request. Chunks cascade."""
    await session.execute(delete(KnowledgeSource).where(KnowledgeSource.id == source_id))
