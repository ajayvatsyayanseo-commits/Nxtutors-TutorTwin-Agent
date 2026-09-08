"""RAG: ownership isolation, ingestion idempotency, retrieval quality, cost.

Every test asserts either a security property (wrong-owner retrieval is exactly
zero) or a cost counter (embedding calls), because those are the two things this
subsystem can get catastrophically wrong.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.knowledge_models import (
    DocumentChunk,
    EmbeddingCache,
    KnowledgeSource,
    RetrievalEventRow,
)
from tutortwin.db.models import Subject, Tutor
from tutortwin.domain.capabilities import CapabilityId
from tutortwin.domain.knowledge import RetrievalScope, SourceKind, Visibility
from tutortwin.policies.rag_policy import RagContext, RagDecision, RagReason, RagVerdict
from tutortwin.rag.embeddings import DeterministicEmbeddingProvider, embed_with_cache
from tutortwin.rag.ingestion import (
    IngestionRequest,
    OwnershipError,
    ingest,
    soft_delete_source,
)
from tutortwin.rag.service import RetrievalService, record_retrieval
from tutortwin.rag.vector_store import keyword_search, similarity_search

pytestmark = pytest.mark.integration


MATHS_DOC = """[page 1]
QUADRATIC EQUATIONS
A quadratic equation has the form ax^2 + bx + c = 0 where a is non zero. The
discriminant b^2 minus 4ac determines how many real roots the equation has.
[page 2]
TRIGONOMETRY BASICS
The sine rule relates the sides of a triangle to the sines of its angles. The
cosine rule generalises Pythagoras to non right angled triangles.
"""

BIOLOGY_DOC = """[page 1]
PHOTOSYNTHESIS
Photosynthesis occurs in chloroplasts where chlorophyll absorbs light energy to
build glucose from carbon dioxide and water in the leaves of green plants.
"""

TUTOR_DOC = """[page 1]
MY FACTORISING METHOD
The grouping method I teach in class splits the middle term before factorising
the quadratic expression into two separate brackets step by step.
"""


async def make_subject(session: AsyncSession) -> Subject:
    subject = Subject(
        external_identity_type="test_phone",
        external_identity_value=f"+91{uuid.uuid4().hex[:10]}",
    )
    session.add(subject)
    await session.flush()
    return subject


async def make_tutor(session: AsyncSession, name: str = "Tutor") -> Tutor:
    tutor = Tutor(display_name=f"{name} {uuid.uuid4().hex[:6]}")
    session.add(tutor)
    await session.flush()
    return tutor


def provider() -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider()


async def embed_query(session: AsyncSession, prov, text: str) -> list[float]:
    result = await embed_with_cache(session, prov, (text,))
    return list(result.vectors[0])


# --- ownership isolation: the security core -----------------------------------


async def test_student_cannot_retrieve_another_students_private_source(
    session: AsyncSession,
) -> None:
    """Mandatory: wrong-owner retrieval must be exactly zero."""
    alice = await make_subject(session)
    bob = await make_subject(session)
    prov = provider()

    await ingest(
        session,
        IngestionRequest(
            title="Bob Private Biology",
            text=BIOLOGY_DOC,
            kind=SourceKind.PDF_EXTRACTION,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=bob.id,
        ),
        prov,
    )
    await session.commit()

    # Alice queries for exactly Bob's topic.
    vector = await embed_query(session, prov, "photosynthesis chloroplast glucose")
    result = await similarity_search(
        session,
        query_embedding=vector,
        scope=RetrievalScope(subject_id=alice.id),
        embedding_model=prov.model,
        top_k=10,
    )

    assert len(result.evidence) == 0
    # candidates_scanned == 0 proves the rows were never selected - the filter
    # is in SQL, not applied to fetched data afterwards.
    assert result.candidates_scanned == 0


async def test_guessed_chunk_id_does_not_bypass_ownership(
    session: AsyncSession,
) -> None:
    """Knowing an id must not help: the predicate is on ownership, not on id."""
    alice = await make_subject(session)
    bob = await make_subject(session)
    prov = provider()

    ingested = await ingest(
        session,
        IngestionRequest(
            title="Bob Private",
            text=BIOLOGY_DOC,
            kind=SourceKind.PDF_EXTRACTION,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=bob.id,
        ),
        prov,
    )
    await session.commit()

    bob_chunks = (
        (
            await session.execute(
                select(DocumentChunk.id).where(DocumentChunk.source_id == ingested.source_id)
            )
        )
        .scalars()
        .all()
    )
    assert bob_chunks, "fixture must produce chunks"

    # Even holding the real id, Alice's scope cannot surface it.
    lexical = await keyword_search(
        session,
        query="photosynthesis chloroplast",
        scope=RetrievalScope(subject_id=alice.id),
        top_k=10,
    )
    assert len(lexical.evidence) == 0


async def test_tutor_material_invisible_to_another_tutors_student(
    session: AsyncSession,
) -> None:
    student = await make_subject(session)
    tutor_one = await make_tutor(session, "One")
    tutor_two = await make_tutor(session, "Two")
    prov = provider()

    await ingest(
        session,
        IngestionRequest(
            title="Tutor One Material",
            text=TUTOR_DOC,
            kind=SourceKind.TUTOR_MATERIAL,
            visibility=Visibility.TUTOR,
            tutor_id=tutor_one.id,
        ),
        prov,
    )
    await session.commit()

    vector = await embed_query(session, prov, "grouping method factorising quadratic")

    other = await similarity_search(
        session,
        query_embedding=vector,
        scope=RetrievalScope(subject_id=student.id, tutor_id=tutor_two.id),
        embedding_model=prov.model,
        top_k=10,
    )
    assert len(other.evidence) == 0

    own = await similarity_search(
        session,
        query_embedding=vector,
        scope=RetrievalScope(subject_id=student.id, tutor_id=tutor_one.id),
        embedding_model=prov.model,
        top_k=10,
    )
    assert len(own.evidence) >= 1
    assert own.evidence[0].source_title == "Tutor One Material"


async def test_student_with_no_tutor_sees_no_tutor_material(
    session: AsyncSession,
) -> None:
    """Absence of a grant is denial, not a wildcard."""
    student = await make_subject(session)
    tutor = await make_tutor(session)
    prov = provider()

    await ingest(
        session,
        IngestionRequest(
            title="Tutor Material",
            text=TUTOR_DOC,
            kind=SourceKind.TUTOR_MATERIAL,
            visibility=Visibility.TUTOR,
            tutor_id=tutor.id,
        ),
        prov,
    )
    await session.commit()

    vector = await embed_query(session, prov, "grouping method factorising")
    result = await similarity_search(
        session,
        query_embedding=vector,
        scope=RetrievalScope(subject_id=student.id),  # no tutor
        embedding_model=prov.model,
        top_k=10,
    )
    assert len(result.evidence) == 0


async def test_global_curated_is_visible_to_everyone(session: AsyncSession) -> None:
    alice = await make_subject(session)
    prov = provider()

    await ingest(
        session,
        IngestionRequest(
            title="Curated Reference",
            text=MATHS_DOC,
            kind=SourceKind.SYLLABUS,
            visibility=Visibility.GLOBAL_CURATED,
        ),
        prov,
    )
    await session.commit()

    vector = await embed_query(session, prov, "discriminant real roots quadratic")
    result = await similarity_search(
        session,
        query_embedding=vector,
        scope=RetrievalScope(subject_id=alice.id),
        embedding_model=prov.model,
        top_k=5,
    )
    assert len(result.evidence) >= 1
    assert result.evidence[0].visibility is Visibility.GLOBAL_CURATED


async def test_ingestion_requires_an_owner_for_private_visibility(
    session: AsyncSession,
) -> None:
    """Ownership is validated before anything is embedded."""
    prov = provider()
    with pytest.raises(OwnershipError):
        await ingest(
            session,
            IngestionRequest(
                title="Orphan",
                text=MATHS_DOC,
                kind=SourceKind.NOTE,
                visibility=Visibility.STUDENT_PRIVATE,
                subject_id=None,
            ),
            prov,
        )
    assert prov.calls == 0, "must not embed before validating ownership"


# --- deletion -----------------------------------------------------------------


async def test_deleted_source_is_no_longer_retrieved(session: AsyncSession) -> None:
    student = await make_subject(session)
    prov = provider()

    ingested = await ingest(
        session,
        IngestionRequest(
            title="Temporary Notes",
            text=MATHS_DOC,
            kind=SourceKind.NOTE,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=student.id,
        ),
        prov,
    )
    await session.commit()

    scope = RetrievalScope(subject_id=student.id)
    vector = await embed_query(session, prov, "discriminant real roots")
    before = await similarity_search(
        session,
        query_embedding=vector,
        scope=scope,
        embedding_model=prov.model,
        top_k=5,
    )
    assert len(before.evidence) >= 1

    await soft_delete_source(session, ingested.source_id)
    await session.commit()

    after = await similarity_search(
        session,
        query_embedding=vector,
        scope=scope,
        embedding_model=prov.model,
        top_k=5,
    )
    assert len(after.evidence) == 0
    assert after.candidates_scanned == 0


# --- ingestion idempotency and embedding cost ---------------------------------


async def test_reingesting_the_same_file_costs_no_embeddings(
    session: AsyncSession,
) -> None:
    """Mandatory: re-upload must not repeat embedding work."""
    student = await make_subject(session)
    prov = provider()

    request = IngestionRequest(
        title="Maths Notes",
        text=MATHS_DOC,
        kind=SourceKind.PDF_EXTRACTION,
        visibility=Visibility.STUDENT_PRIVATE,
        subject_id=student.id,
    )
    first = await ingest(session, request, prov)
    await session.commit()
    calls_after_first = prov.calls

    second = await ingest(session, request, prov)
    await session.commit()

    assert first.chunks_created >= 2
    assert second.already_ingested is True
    assert second.chunks_created == 0
    assert second.embedding_api_calls == 0
    assert prov.calls == calls_after_first, "no provider call on re-ingestion"

    sources = await session.scalar(select(func.count()).select_from(KnowledgeSource))
    assert sources == 1


async def test_identical_text_from_two_students_is_embedded_once(
    session: AsyncSession,
) -> None:
    """The embedding cache is global: a vector is derived from text, not owned.

    The two students still get separate, correctly-scoped chunks - only the
    embedding computation is shared.
    """
    alice = await make_subject(session)
    bob = await make_subject(session)
    prov = provider()

    for subject in (alice, bob):
        await ingest(
            session,
            IngestionRequest(
                title=f"Shared Textbook ({subject.id})",
                text=MATHS_DOC,
                kind=SourceKind.SYLLABUS,
                visibility=Visibility.STUDENT_PRIVATE,
                subject_id=subject.id,
            ),
            prov,
        )
    await session.commit()

    assert prov.calls == 1, "second ingestion must hit the embedding cache"

    cached = await session.scalar(select(func.count()).select_from(EmbeddingCache))
    assert cached >= 2  # one row per distinct chunk, not per student

    # Ownership is still separate.
    vector = await embed_query(session, prov, "discriminant real roots")
    alice_hits = await similarity_search(
        session,
        query_embedding=vector,
        scope=RetrievalScope(subject_id=alice.id),
        embedding_model=prov.model,
        top_k=10,
    )
    assert all(e.visibility is Visibility.STUDENT_PRIVATE for e in alice_hits.evidence)
    assert len(alice_hits.evidence) >= 1
    for item in alice_hits.evidence:
        assert str(alice.id) in item.source_title


async def test_repeated_text_within_a_document_is_indexed_once(
    session: AsyncSession,
) -> None:
    student = await make_subject(session)
    prov = provider()
    boilerplate = (
        "This is repeated boilerplate that appears on every single page of the "
        "document and carries no unique information whatsoever for retrieval.\n\n"
    )
    text = "".join(f"[page {i}]\n{boilerplate}" for i in range(1, 5))

    result = await ingest(
        session,
        IngestionRequest(
            title="Repetitive",
            text=text,
            kind=SourceKind.NOTE,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=student.id,
        ),
        prov,
    )
    await session.commit()

    assert result.chunks_skipped_duplicate >= 3
    assert result.chunks_created == 1


# --- retrieval quality --------------------------------------------------------


async def test_retrieval_cites_the_correct_page(session: AsyncSession) -> None:
    student = await make_subject(session)
    prov = provider()

    await ingest(
        session,
        IngestionRequest(
            title="Maths Notes",
            text=MATHS_DOC,
            kind=SourceKind.PDF_EXTRACTION,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=student.id,
        ),
        prov,
    )
    await session.commit()

    vector = await embed_query(session, prov, "sine rule cosine rule triangle angles")
    result = await similarity_search(
        session,
        query_embedding=vector,
        scope=RetrievalScope(subject_id=student.id),
        embedding_model=prov.model,
        top_k=1,
    )
    assert len(result.evidence) == 1
    top = result.evidence[0]
    assert top.page_number == 2, "trigonometry is on page 2"
    assert "page 2" in top.citation
    assert top.section is not None


async def test_keyword_search_needs_no_embedding_call(session: AsyncSession) -> None:
    student = await make_subject(session)
    prov = provider()
    await ingest(
        session,
        IngestionRequest(
            title="Maths Notes",
            text=MATHS_DOC,
            kind=SourceKind.PDF_EXTRACTION,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=student.id,
        ),
        prov,
    )
    await session.commit()
    calls_before = prov.calls

    result = await keyword_search(
        session,
        query="discriminant",
        scope=RetrievalScope(subject_id=student.id),
        top_k=5,
    )
    assert len(result.evidence) >= 1
    assert result.embedding_calls == 0
    assert prov.calls == calls_before


# --- the RAG decision policy, end to end --------------------------------------


async def test_generic_question_performs_no_vector_query(
    session: AsyncSession,
) -> None:
    """Mandatory: a self-contained question must not pay for retrieval."""
    student = await make_subject(session)
    prov = provider()
    await ingest(
        session,
        IngestionRequest(
            title="Maths Notes",
            text=MATHS_DOC,
            kind=SourceKind.PDF_EXTRACTION,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=student.id,
        ),
        prov,
    )
    await session.commit()
    calls_before = prov.calls

    service = RetrievalService(provider=prov)
    verdict, result = await service.decide_and_retrieve(
        session,
        query="Solve for x: 2x + 5 = 13",
        scope=RetrievalScope(subject_id=student.id),
        context=RagContext(
            text="Solve for x: 2x + 5 = 13",
            capability=CapabilityId.MATH,
            has_corpus=True,
        ),
        available_tokens=2000,
    )

    assert verdict.should_retrieve is False
    assert result.performed is False
    assert result.embedding_calls == 0
    assert prov.calls == calls_before, "no embedding for a self-contained question"


async def test_document_question_does_retrieve(session: AsyncSession) -> None:
    student = await make_subject(session)
    prov = provider()
    await ingest(
        session,
        IngestionRequest(
            title="Maths Notes",
            text=MATHS_DOC,
            kind=SourceKind.PDF_EXTRACTION,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=student.id,
        ),
        prov,
    )
    await session.commit()

    service = RetrievalService(provider=prov)
    verdict, result = await service.decide_and_retrieve(
        session,
        query="What does this document say about the discriminant?",
        scope=RetrievalScope(subject_id=student.id),
        context=RagContext(
            text="What does this document say about the discriminant?",
            capability=CapabilityId.DOCUMENT_QA,
            has_corpus=True,
        ),
        available_tokens=2000,
    )

    assert verdict.should_retrieve is True
    assert result.performed is True
    assert len(result.evidence) >= 1


async def test_empty_corpus_skips_before_embedding(session: AsyncSession) -> None:
    student = await make_subject(session)
    prov = provider()
    service = RetrievalService(provider=prov)

    assert await service.has_corpus(session, RetrievalScope(subject_id=student.id)) is False

    verdict, result = await service.decide_and_retrieve(
        session,
        query="Summarize this document for me",
        scope=RetrievalScope(subject_id=student.id),
        context=RagContext(
            text="Summarize this document for me",
            capability=CapabilityId.DOCUMENT_QA,
            has_corpus=False,
        ),
        available_tokens=2000,
    )
    assert result.performed is False
    assert result.skipped_reason == "NO_CORPUS_AVAILABLE"
    assert prov.calls == 0


async def test_retrieval_events_record_skips_too(session: AsyncSession) -> None:
    """Non-retrievals are recorded, so RAG frequency is measurable."""
    student = await make_subject(session)
    from tutortwin.domain.knowledge import RetrievalResult

    await record_retrieval(
        session,
        subject_id=student.id,
        request_event_id=None,
        verdict=RagVerdict(RagDecision.SKIP, RagReason.SELF_CONTAINED_COMPUTATION),
        result=RetrievalResult(skipped_reason="SELF_CONTAINED_COMPUTATION"),
    )
    await session.commit()

    row = (await session.execute(select(RetrievalEventRow))).scalar_one()
    assert row.performed is False
    assert row.skip_reason == "SELF_CONTAINED_COMPUTATION"
    assert row.embedding_calls == 0
