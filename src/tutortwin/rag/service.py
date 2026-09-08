"""Retrieval orchestration: decide, embed, search, fuse, record.

The order matters for cost. The policy decides *first*, so a question that does
not need retrieval never reaches the embedding call - the skip is free, and it
is the common case.

Keyword search runs before the vector search because it costs nothing; when it
alone answers well, the embedding call is avoided entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.knowledge_models import DocumentChunk, RetrievalEventRow
from tutortwin.domain.knowledge import (
    RetrievalResult,
    RetrievalScope,
)
from tutortwin.observability.logging import get_logger
from tutortwin.policies.rag_policy import RagContext, RagVerdict, decide, dynamic_top_k
from tutortwin.rag.embeddings import EmbeddingProvider, embed_with_cache
from tutortwin.rag.vector_store import (
    build_visibility_predicate,
    keyword_search,
    reciprocal_rank_fusion,
    similarity_search,
)

logger = get_logger(__name__)

KEYWORD_SUFFICIENT_HITS = 3
"""When exact-term search already returns this many owned passages, the vector
search adds ranking nuance that rarely changes the answer - and costs a call."""


@dataclass(slots=True)
class RetrievalService:
    provider: EmbeddingProvider

    async def has_corpus(self, session: AsyncSession, scope: RetrievalScope) -> bool:
        """Does this scope own anything at all?

        Checked before deciding, because retrieval against an empty corpus still
        pays for the query embedding and can only return nothing.
        """
        predicate = build_visibility_predicate(scope)
        from sqlalchemy import text as sql_text

        # `predicate.sql` is code-generated from a closed set of literals; all
        # values travel as bound parameters (RetrievalScope types reject
        # non-UUID input before it can reach SQL).
        statement = sql_text(
            "SELECT 1 FROM document_chunks c "
            "JOIN knowledge_sources s ON s.id = c.source_id "
            f"WHERE {predicate.sql} AND s.deleted_at IS NULL LIMIT 1"
        )
        found = await session.execute(statement, predicate.params)
        return found.scalar_one_or_none() is not None

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        query: str,
        scope: RetrievalScope,
        verdict: RagVerdict,
        available_tokens: int,
    ) -> RetrievalResult:
        """Execute a retrieval the policy already approved."""
        if not verdict.should_retrieve:
            return RetrievalResult(skipped_reason=verdict.reason.value)

        top_k = dynamic_top_k(verdict.top_k, available_tokens=available_tokens)
        if top_k <= 0:
            return RetrievalResult(skipped_reason="no_context_budget_for_evidence")

        # Free first.
        lexical = await keyword_search(session, query=query, scope=scope, top_k=top_k)
        if len(lexical.evidence) >= KEYWORD_SUFFICIENT_HITS:
            logger.info(
                "retrieval_keyword_sufficient",
                hits=len(lexical.evidence),
                embedding_calls=0,
            )
            return RetrievalResult(
                evidence=lexical.evidence[:top_k],
                query_ms=lexical.query_ms,
                candidates_scanned=lexical.candidates_scanned,
                embedding_calls=0,
            )

        embedding = await embed_with_cache(session, self.provider, (query,))
        vector = await similarity_search(
            session,
            query_embedding=list(embedding.vectors[0]),
            scope=scope,
            embedding_model=self.provider.model,
            top_k=top_k,
        )

        fused = reciprocal_rank_fusion(vector, lexical, limit=top_k)
        return RetrievalResult(
            evidence=fused,
            query_ms=vector.query_ms + lexical.query_ms,
            candidates_scanned=vector.candidates_scanned,
            embedding_calls=embedding.api_calls,
        )

    async def decide_and_retrieve(
        self,
        session: AsyncSession,
        *,
        query: str,
        scope: RetrievalScope,
        context: RagContext,
        available_tokens: int,
    ) -> tuple[RagVerdict, RetrievalResult]:
        """The full path: policy, then retrieval only if it earned its cost."""
        verdict = decide(context)
        result = await self.retrieve(
            session,
            query=query,
            scope=scope,
            verdict=verdict,
            available_tokens=available_tokens,
        )
        return verdict, result


async def record_retrieval(
    session: AsyncSession,
    *,
    subject_id: UUID,
    request_event_id: UUID | None,
    verdict: RagVerdict,
    result: RetrievalResult,
) -> None:
    """Audit every decision, including the skips.

    Recording non-retrievals is what makes "how often does RAG actually run"
    answerable from data rather than from an assumption.
    """
    session.add(
        RetrievalEventRow(
            subject_id=subject_id,
            request_event_id=request_event_id,
            performed=result.performed,
            skip_reason=result.skipped_reason,
            top_k=verdict.top_k,
            returned=len(result.evidence),
            candidates_scanned=result.candidates_scanned,
            embedding_calls=result.embedding_calls,
            query_ms=result.query_ms,
            chunk_ids_json=[str(e.chunk_id) for e in result.evidence],
        )
    )


async def count_owned_chunks(session: AsyncSession, scope: RetrievalScope) -> int:
    """Owned corpus size. Used by the evaluation harness and admin views."""
    predicate = build_visibility_predicate(scope)
    from sqlalchemy import text as sql_text

    # Generated predicate, bound parameters only - see has_corpus.
    statement = sql_text(
        "SELECT COUNT(*) FROM document_chunks c "
        "JOIN knowledge_sources s ON s.id = c.source_id "
        f"WHERE {predicate.sql} AND s.deleted_at IS NULL"
    )
    return int((await session.execute(statement, predicate.params)).scalar_one())


async def chunk_ids_for_source(session: AsyncSession, source_id: UUID) -> list[UUID]:
    rows = (
        await session.execute(select(DocumentChunk.id).where(DocumentChunk.source_id == source_id))
    ).scalars()
    return list(rows)
