"""Vector search with ownership enforced in SQL.

**The security rule.** The visibility predicate and the similarity ranking are
parts of the same statement. Private content is never loaded into this process
and then filtered - a missed filter would be a cross-student leak, and rows that
were never selected cannot leak.

**On pgvector.** The extension is not installed in every environment (it needs a
compiler, which a managed or minimal host may not have). Rather than fail, the
similarity is computed in SQL over a JSONB array:

    cosine_distance = 1 - dot(a, b) / (norm(a) * norm(b))

which needs nothing but core Postgres. The trade is honest and worth stating:

* correctness - identical results either way, this is the same arithmetic
* security    - identical, the predicate is in the same WHERE clause
* speed       - a sequential scan of the *owned* subset, not an ANN index

That is fine at a per-student corpus size, where the ownership predicate already
reduces candidates to tens or hundreds. It is not fine at a million shared
chunks. `pgvector_available()` reports which mode is live, and
`PGVECTOR_UPGRADE` documents the migration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.domain.knowledge import (
    RetrievalEvidence,
    RetrievalResult,
    RetrievalScope,
    Visibility,
)
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

SNIPPET_CHARS = 400

PGVECTOR_UPGRADE = """\
To move to an ANN index once pgvector is available:
  1. CREATE EXTENSION vector;
  2. ALTER TABLE document_chunks ADD COLUMN embedding vector(N);
  3. backfill from embedding_json;
  4. CREATE INDEX ... USING hnsw (embedding vector_cosine_ops);
  5. swap the distance expression in _SIMILARITY_SQL for `embedding <=> :q`.
The ownership predicate does not change - it is already in the WHERE clause."""


async def pgvector_available(session: AsyncSession) -> bool:
    result = await session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    return result.scalar_one_or_none() is not None


# Cosine distance over a JSONB array, in pure SQL. Sub-selects over
# jsonb_array_elements give the dot product and both norms.
_COSINE_DISTANCE = """
1 - (
    (SELECT COALESCE(SUM((a.v)::float8 * (b.v)::float8), 0)
       FROM jsonb_array_elements_text(c.embedding_json) WITH ORDINALITY AS a(v, i)
       JOIN jsonb_array_elements_text(CAST(:query_vec AS jsonb)) WITH ORDINALITY AS b(v, i)
         ON a.i = b.i)
    / NULLIF(
        SQRT((SELECT COALESCE(SUM(((x.v)::float8) ^ 2), 0)
                FROM jsonb_array_elements_text(c.embedding_json) AS x(v)))
      * SQRT((SELECT COALESCE(SUM(((y.v)::float8) ^ 2), 0)
                FROM jsonb_array_elements_text(CAST(:query_vec AS jsonb)) AS y(v))),
      0)
)
"""


@dataclass(frozen=True, slots=True)
class _Predicate:
    sql: str
    params: dict[str, object]


def build_visibility_predicate(scope: RetrievalScope) -> _Predicate:
    """Compile a scope into a SQL WHERE fragment.

    Every branch is an explicit grant. A scope with no tutor produces no TUTOR
    clause at all, so tutor material is unreachable rather than merely unranked.
    """
    clauses: list[str] = []
    params: dict[str, object] = {}

    # A student's own private material.
    clauses.append("(c.visibility = 'STUDENT_PRIVATE' AND c.subject_id = :subject_id)")
    params["subject_id"] = scope.subject_id

    if scope.include_global:
        clauses.append("(c.visibility = 'GLOBAL_CURATED')")

    if scope.tutor_id is not None:
        clauses.append("(c.visibility = 'TUTOR' AND c.tutor_id = :tutor_id)")
        params["tutor_id"] = scope.tutor_id

    if scope.course_ids:
        clauses.append("(c.visibility = 'COURSE' AND c.course_id = ANY(:course_ids))")
        params["course_ids"] = [str(cid) for cid in scope.course_ids]

    if scope.conversation_id is not None:
        clauses.append("(c.visibility = 'CONVERSATION' AND c.conversation_id = :conversation_id)")
        params["conversation_id"] = scope.conversation_id

    return _Predicate(sql="(" + " OR ".join(clauses) + ")", params=params)


_SIMILARITY_SQL = """
SELECT c.id            AS chunk_id,
       c.source_id     AS source_id,
       s.title         AS source_title,
       c.visibility    AS visibility,
       c.page_number   AS page_number,
       c.section       AS section,
       LEFT(c.text, :snippet_chars) AS snippet,
       {distance}      AS distance
  FROM document_chunks c
  JOIN knowledge_sources s ON s.id = c.source_id
 WHERE {visibility}
   AND s.deleted_at IS NULL
   AND c.embedding_json IS NOT NULL
   AND c.embedding_model = :embedding_model
 ORDER BY distance ASC
 LIMIT :top_k
"""

_COUNT_CANDIDATES_SQL = """
SELECT COUNT(*)
  FROM document_chunks c
  JOIN knowledge_sources s ON s.id = c.source_id
 WHERE {visibility}
   AND s.deleted_at IS NULL
"""


async def similarity_search(
    session: AsyncSession,
    *,
    query_embedding: list[float],
    scope: RetrievalScope,
    embedding_model: str,
    top_k: int,
) -> RetrievalResult:
    """Rank owned chunks by cosine similarity. Ownership is in the WHERE clause."""
    if top_k <= 0 or not query_embedding:
        return RetrievalResult(skipped_reason="empty_query_or_zero_k")

    predicate = build_visibility_predicate(scope)
    started = time.monotonic()

    import json

    params: dict[str, object] = {
        **predicate.params,
        "query_vec": json.dumps(query_embedding),
        "embedding_model": embedding_model,
        "top_k": top_k,
        "snippet_chars": SNIPPET_CHARS,
    }

    statement = text(_SIMILARITY_SQL.format(distance=_COSINE_DISTANCE, visibility=predicate.sql))
    if "course_ids" in params:
        statement = statement.bindparams(bindparam("course_ids", expanding=False))

    rows = (await session.execute(statement, params)).mappings().all()

    count_statement = text(_COUNT_CANDIDATES_SQL.format(visibility=predicate.sql))
    candidate_params = {k: v for k, v in params.items() if k in predicate.params}
    candidates = (await session.execute(count_statement, candidate_params)).scalar_one()

    evidence = tuple(
        RetrievalEvidence(
            chunk_id=row["chunk_id"],
            source_id=row["source_id"],
            source_title=row["source_title"],
            visibility=Visibility(row["visibility"]),
            # Report similarity, not distance: a bigger number meaning "better"
            # is what every caller and log reader expects.
            score=round(1.0 - float(row["distance"]), 6),
            snippet=row["snippet"],
            page_number=row["page_number"],
            section=row["section"],
        )
        for row in rows
    )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "similarity_search",
        returned=len(evidence),
        candidates=int(candidates),
        top_k=top_k,
        query_ms=elapsed_ms,
        visible_kinds=[v.value for v in scope.visible_kinds],
    )
    return RetrievalResult(
        evidence=evidence, query_ms=elapsed_ms, candidates_scanned=int(candidates)
    )


async def keyword_search(
    session: AsyncSession,
    *,
    query: str,
    scope: RetrievalScope,
    top_k: int,
) -> RetrievalResult:
    """Postgres full-text search under the same ownership predicate.

    Free - no embedding call - and better than vectors at exact terms: a student
    asking for "Theorem 4.2" wants the literal string, which embeddings blur.
    """
    if top_k <= 0 or not query.strip():
        return RetrievalResult(skipped_reason="empty_query_or_zero_k")

    predicate = build_visibility_predicate(scope)
    started = time.monotonic()

    # Generated predicate, bound parameters only - see build_visibility_predicate.
    statement = text(
        f"""
        SELECT c.id AS chunk_id, c.source_id, s.title AS source_title,
               c.visibility, c.page_number, c.section,
               LEFT(c.text, :snippet_chars) AS snippet,
               ts_rank(to_tsvector('english', c.text),
                       plainto_tsquery('english', :query)) AS rank
          FROM document_chunks c
          JOIN knowledge_sources s ON s.id = c.source_id
         WHERE {predicate.sql}
           AND s.deleted_at IS NULL
           AND to_tsvector('english', c.text) @@ plainto_tsquery('english', :query)
         ORDER BY rank DESC
         LIMIT :top_k
        """
    )
    params: dict[str, object] = {
        **predicate.params,
        "query": query,
        "top_k": top_k,
        "snippet_chars": SNIPPET_CHARS,
    }
    rows = (await session.execute(statement, params)).mappings().all()

    evidence = tuple(
        RetrievalEvidence(
            chunk_id=row["chunk_id"],
            source_id=row["source_id"],
            source_title=row["source_title"],
            visibility=Visibility(row["visibility"]),
            score=round(float(row["rank"]), 6),
            snippet=row["snippet"],
            page_number=row["page_number"],
            section=row["section"],
        )
        for row in rows
    )
    return RetrievalResult(
        evidence=evidence,
        query_ms=int((time.monotonic() - started) * 1000),
        candidates_scanned=len(evidence),
        embedding_calls=0,
    )


def reciprocal_rank_fusion(
    *results: RetrievalResult, k: int = 60, limit: int = 10
) -> tuple[RetrievalEvidence, ...]:
    """Merge rankings without comparing incomparable scores.

    Cosine similarity and ts_rank are on different scales; averaging them is
    meaningless. RRF uses only rank position, so a chunk that both methods place
    near the top wins - which is exactly the signal hybrid search is after.
    """
    scores: dict[UUID, float] = {}
    best: dict[UUID, RetrievalEvidence] = {}

    for result in results:
        for rank, item in enumerate(result.evidence, start=1):
            scores[item.chunk_id] = scores.get(item.chunk_id, 0.0) + 1.0 / (k + rank)
            if item.chunk_id not in best:
                best[item.chunk_id] = item

    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], str(pair[0])))
    return tuple(
        best[chunk_id].model_copy(update={"score": round(score, 6)})
        for chunk_id, score in ordered[:limit]
    )
