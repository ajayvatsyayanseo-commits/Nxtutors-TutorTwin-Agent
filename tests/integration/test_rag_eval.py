"""RAG evaluation on a small deterministic corpus.

A regression harness, not a benchmark. It fixes measurable numbers - recall,
citation accuracy, wrong-owner retrieval, embedding calls - so a change that
quietly degrades retrieval fails a test instead of shipping.

Wrong-owner recall has a hard threshold: it must be exactly zero. Every other
metric has a floor that current behaviour clears with margin.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.models import Subject, Tutor
from tutortwin.domain.knowledge import RetrievalScope, SourceKind, Visibility
from tutortwin.rag.embeddings import DeterministicEmbeddingProvider
from tutortwin.rag.ingestion import IngestionRequest, ingest
from tutortwin.rag.service import RetrievalService, count_owned_chunks

pytestmark = pytest.mark.integration


# --- the corpus ---------------------------------------------------------------

ALGEBRA = """[page 1]
QUADRATIC EQUATIONS
A quadratic equation takes the form ax squared plus bx plus c equals zero where
a is not zero. The discriminant b squared minus four ac tells you how many real
roots the equation has before you solve it.
[page 2]
FACTORISING METHODS
Factorising splits a quadratic into two brackets whose product is the original
expression. Completing the square rewrites it so the variable appears once.
"""

GEOMETRY = """[page 1]
TRIANGLE RULES
The sine rule relates each side of a triangle to the sine of its opposite angle.
The cosine rule generalises Pythagoras to triangles without a right angle.
[page 2]
CIRCLE THEOREMS
The angle at the centre of a circle is twice the angle at the circumference when
both are subtended by the same arc of that circle.
"""

BIOLOGY = """[page 1]
PHOTOSYNTHESIS
Photosynthesis takes place in the chloroplasts where chlorophyll absorbs light
energy and converts carbon dioxide and water into glucose and oxygen.
[page 2]
RESPIRATION
Aerobic respiration releases energy from glucose using oxygen and produces
carbon dioxide and water as the waste products of the reaction.
"""

TUTOR_MATERIAL = """[page 1]
MY MARKING SCHEME
When marking algebra I award method marks for a correct approach even when the
final arithmetic is wrong, so always show every line of your working clearly.
"""


@dataclass(frozen=True, slots=True)
class EvalCase:
    query: str
    expected_source: str
    expected_page: int


# Each query names material that exists in exactly one place, so recall and
# citation accuracy are unambiguous.
EVAL_CASES: tuple[EvalCase, ...] = (
    EvalCase("discriminant real roots quadratic equation", "Algebra", 1),
    EvalCase("factorising brackets completing the square", "Algebra", 2),
    EvalCase("sine rule cosine rule triangle angle", "Geometry", 1),
    EvalCase("angle at the centre circle circumference arc", "Geometry", 2),
    EvalCase("chloroplasts chlorophyll glucose oxygen light", "Biology", 1),
    EvalCase("aerobic respiration releases energy carbon dioxide", "Biology", 2),
)

RECALL_FLOOR = 0.80
CITATION_FLOOR = 0.80


async def _seed(
    session: AsyncSession,
) -> tuple[Subject, Subject, Tutor, DeterministicEmbeddingProvider]:
    owner = Subject(
        external_identity_type="test_phone",
        external_identity_value=f"+91{uuid.uuid4().hex[:10]}",
    )
    intruder = Subject(
        external_identity_type="test_phone",
        external_identity_value=f"+91{uuid.uuid4().hex[:10]}",
    )
    tutor = Tutor(display_name=f"Tutor {uuid.uuid4().hex[:6]}")
    session.add_all([owner, intruder, tutor])
    await session.flush()

    provider = DeterministicEmbeddingProvider()
    for title, text in (("Algebra", ALGEBRA), ("Geometry", GEOMETRY), ("Biology", BIOLOGY)):
        await ingest(
            session,
            IngestionRequest(
                title=title,
                text=text,
                kind=SourceKind.PDF_EXTRACTION,
                visibility=Visibility.STUDENT_PRIVATE,
                subject_id=owner.id,
            ),
            provider,
        )
    await ingest(
        session,
        IngestionRequest(
            title="Tutor Marking",
            text=TUTOR_MATERIAL,
            kind=SourceKind.TUTOR_MATERIAL,
            visibility=Visibility.TUTOR,
            tutor_id=tutor.id,
        ),
        provider,
    )
    await session.commit()
    return owner, intruder, tutor, provider


async def test_corpus_ingests_completely(session: AsyncSession) -> None:
    owner, _, _, _ = await _seed(session)
    chunks = await count_owned_chunks(session, RetrievalScope(subject_id=owner.id))
    # Three documents, two pages each, one chunk per page.
    assert chunks >= 6


async def test_retrieval_recall_meets_the_floor(session: AsyncSession) -> None:
    """The right source must be retrieved for a targeted query."""
    owner, _, _, provider = await _seed(session)
    service = RetrievalService(provider=provider)
    scope = RetrievalScope(subject_id=owner.id)

    hits = 0
    for case in EVAL_CASES:
        result = await service.retrieve(
            session,
            query=case.query,
            scope=scope,
            verdict=_always_retrieve(),
            available_tokens=4000,
        )
        titles = {e.source_title for e in result.evidence}
        if case.expected_source in titles:
            hits += 1

    recall = hits / len(EVAL_CASES)
    assert recall >= RECALL_FLOOR, f"recall {recall:.2f} below floor {RECALL_FLOOR}"


async def test_top_result_cites_the_right_page(session: AsyncSession) -> None:
    owner, _, _, provider = await _seed(session)
    service = RetrievalService(provider=provider)
    scope = RetrievalScope(subject_id=owner.id)

    correct = 0
    for case in EVAL_CASES:
        result = await service.retrieve(
            session,
            query=case.query,
            scope=scope,
            verdict=_always_retrieve(top_k=1),
            available_tokens=4000,
        )
        if not result.evidence:
            continue
        top = result.evidence[0]
        if top.source_title == case.expected_source and top.page_number == case.expected_page:
            correct += 1

    accuracy = correct / len(EVAL_CASES)
    assert accuracy >= CITATION_FLOOR, f"citation accuracy {accuracy:.2f} too low"


async def test_wrong_owner_recall_is_exactly_zero(session: AsyncSession) -> None:
    """The one metric with no tolerance: a leak is a leak."""
    owner, intruder, _, provider = await _seed(session)
    service = RetrievalService(provider=provider)
    intruder_scope = RetrievalScope(subject_id=intruder.id)

    leaked = 0
    for case in EVAL_CASES:
        result = await service.retrieve(
            session,
            query=case.query,
            scope=intruder_scope,
            verdict=_always_retrieve(top_k=10),
            available_tokens=4000,
        )
        leaked += len(result.evidence)

    assert leaked == 0, f"{leaked} chunks leaked to a non-owner"


async def test_tutor_material_needs_the_tutor_grant(session: AsyncSession) -> None:
    owner, _, tutor, provider = await _seed(session)
    service = RetrievalService(provider=provider)
    query = "method marks marking scheme working"

    without = await service.retrieve(
        session,
        query=query,
        scope=RetrievalScope(subject_id=owner.id),
        verdict=_always_retrieve(top_k=10),
        available_tokens=4000,
    )
    assert all(e.source_title != "Tutor Marking" for e in without.evidence)

    with_grant = await service.retrieve(
        session,
        query=query,
        scope=RetrievalScope(subject_id=owner.id, tutor_id=tutor.id),
        verdict=_always_retrieve(top_k=10),
        available_tokens=4000,
    )
    assert any(e.source_title == "Tutor Marking" for e in with_grant.evidence)


async def test_query_latency_is_reasonable(session: AsyncSession) -> None:
    """A regression guard on the JSONB cosine path, not a performance claim."""
    owner, _, _, provider = await _seed(session)
    service = RetrievalService(provider=provider)

    result = await service.retrieve(
        session,
        query=EVAL_CASES[0].query,
        scope=RetrievalScope(subject_id=owner.id),
        verdict=_always_retrieve(),
        available_tokens=4000,
    )
    assert result.query_ms < 2000, f"retrieval took {result.query_ms}ms"


async def test_context_size_stays_bounded(session: AsyncSession) -> None:
    """Retrieved evidence must fit the budget it was sized for."""
    owner, _, _, provider = await _seed(session)
    service = RetrievalService(provider=provider)

    result = await service.retrieve(
        session,
        query="discriminant roots factorising brackets triangle circle",
        scope=RetrievalScope(subject_id=owner.id),
        verdict=_always_retrieve(top_k=8),
        available_tokens=900,
    )
    # dynamic_top_k caps k, so the snippets cannot overflow the allowance.
    assert len(result.evidence) <= 5
    assert result.total_snippet_chars <= 5 * 400


async def test_repeat_query_reuses_the_embedding_cache(session: AsyncSession) -> None:
    owner, _, _, provider = await _seed(session)
    service = RetrievalService(provider=provider)
    scope = RetrievalScope(subject_id=owner.id)
    query = "discriminant tells how many real roots exist"

    first = await service.retrieve(
        session, query=query, scope=scope, verdict=_always_retrieve(), available_tokens=4000
    )
    await session.commit()
    calls_after_first = provider.calls

    second = await service.retrieve(
        session, query=query, scope=scope, verdict=_always_retrieve(), available_tokens=4000
    )
    await session.commit()

    assert second.embedding_calls == 0
    assert provider.calls == calls_after_first
    assert {e.chunk_id for e in first.evidence} == {e.chunk_id for e in second.evidence}


def _always_retrieve(top_k: int = 5):
    """Bypass the policy so retrieval quality is measured, not the gate."""
    from tutortwin.policies.rag_policy import RagDecision, RagReason, RagVerdict

    return RagVerdict(RagDecision.RETRIEVE, RagReason.REFERS_TO_UPLOAD, top_k=top_k)
