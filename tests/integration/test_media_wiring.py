"""An attachment arriving on the event API reaches the media pipeline.

This is the seam Phase 03 left open: `MediaPipeline` was built and tested, and
nothing called it from `handle_event`. The orchestrator implemented its own copy
of the brief gate and then dropped into the text path, so an inbound PDF created
no `media_objects` row and enqueued no job - and `/internal/jobs/run` processed a
job that nothing was creating.

These tests assert the connection, not the pipeline's internals. They fail if the
wiring is removed, which the pipeline's own suite would not notice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutortwin.db.models import Job, MediaObject
from tutortwin.domain.events import (
    InboundMessage,
    MediaRef,
    MessageType,
    NormalizedEvent,
    SubjectRef,
)
from tutortwin.domain.media import MediaState, RejectReason
from tutortwin.media.adapters import InMemoryMediaSource, RecordingTaskQueue
from tutortwin.media.blobstore import FilesystemBlobStore
from tutortwin.media.ocr import TesseractOCRProvider
from tutortwin.media.pipeline import MediaPipeline
from tutortwin.orchestration.entry_service import EntryDependencies, TutorTwinEntryService
from tutortwin.providers.fakes import (
    FakeEntitlementGateway,
    FakeIdentityGateway,
    FakeOutboundGateway,
    FakeTutorGateway,
    FixedClock,
)

pytestmark = pytest.mark.integration

PRO = "+919999000001"
FREE = "+919999000002"

# A one-page PDF. Real magic bytes, because validation sniffs rather than trusts.
PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def media_event(message_id: str, *, text: str | None, identity: str = PRO) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"evt_{message_id}",
        request_id=f"req_{message_id}",
        correlation_id=f"corr_{message_id}",
        source="wiring",
        subject=SubjectRef(external_type="test_phone", external_id=identity),
        message=InboundMessage(
            message_id=message_id,
            type=MessageType.PDF,
            text=text,
            media=MediaRef(
                provider="test",
                media_id="notes.pdf",
                mime_type_hint="application/pdf",
                size_hint=len(PDF_BYTES),
                filename="notes.pdf",
            ),
        ),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def build(
    session_factory: async_sessionmaker[AsyncSession], tmp: Path
) -> tuple[TutorTwinEntryService, RecordingTaskQueue, InMemoryMediaSource]:
    source = InMemoryMediaSource()
    source.add("notes.pdf", PDF_BYTES)
    queue = RecordingTaskQueue()
    pipeline = MediaPipeline(
        source=source,
        blobstore=FilesystemBlobStore(root=tmp / "objects"),
        ocr=TesseractOCRProvider(),
        queue=queue,
    )
    service = TutorTwinEntryService(
        EntryDependencies(
            identity=FakeIdentityGateway(),
            entitlement=FakeEntitlementGateway(plans={PRO: "PRO"}),
            tutor=FakeTutorGateway(),
            outbound=FakeOutboundGateway(),
            clock=FixedClock(),
            session_factory=session_factory,
            gateway_factory=None,
            media_pipeline=pipeline,
        )
    )
    return service, queue, source


async def test_an_attachment_with_no_brief_is_recorded_and_never_fetched(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The zero-cost hold, now with a row to show for it.

    Before the wiring there was no row at all: the orchestrator answered the
    brief prompt and forgot the file existed, so the brief that arrived next
    turn had nothing to attach to.
    """
    service, queue, source = build(session_factory, tmp_path)

    response = await service.handle_event(media_event(f"{uuid4().hex[:8]}-hold", text=None))

    assert any(a.type == "ASK_FILE_BRIEF" for a in response.outbound_actions)
    assert source.fetch_count == 0, "a held attachment must not be downloaded"
    assert queue.depth == 0, "and must not be queued for extraction"

    media = (await session.execute(select(MediaObject))).scalars().all()
    assert len(media) == 1, "the file is remembered, so the next turn can act on it"
    assert MediaState(media[0].state) is MediaState.WAITING_FOR_BRIEF


async def test_an_attachment_with_a_brief_is_queued_for_extraction(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The connection that did not exist: a brief creates a real job.

    `/internal/jobs/run` has always been able to process a MEDIA_EXTRACT job.
    Nothing was creating one.
    """
    service, queue, _ = build(session_factory, tmp_path)

    response = await service.handle_event(
        media_event(f"{uuid4().hex[:8]}-brief", text="summarise page 1")
    )

    jobs = (await session.execute(select(Job))).scalars().all()
    assert len(jobs) == 1, "a briefed attachment must produce exactly one job"
    assert jobs[0].job_type == "MEDIA_EXTRACT"
    assert queue.depth == 1, "and that job must be dispatched"
    assert response.outbound_actions, "the student is told the file is being read"


async def test_a_redelivered_attachment_does_not_queue_a_second_job(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """At-least-once delivery must not become at-least-twice extraction."""
    service, queue, _ = build(session_factory, tmp_path)
    event = media_event(f"{uuid4().hex[:8]}-dup", text="summarise page 1")

    await service.handle_event(event)
    await service.handle_event(event)

    jobs = (await session.execute(select(func.count()).select_from(Job))).scalar_one()
    assert jobs == 1
    assert queue.depth == 1


async def test_an_unentitled_student_never_reaches_the_fetch(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Entitlement is the first gate, and it runs before a single byte moves.

    It fires in the orchestrator, *above* intake, so an unentitled attachment
    does not even reach the media pipeline - there is no `media_objects` row,
    because creating one would be work done for a request that was always going
    to be refused. The pipeline has its own entitlement check as well; this
    asserts the cheaper outer one, which is the one that actually runs.
    """
    service, queue, source = build(session_factory, tmp_path)

    response = await service.handle_event(
        media_event(f"{uuid4().hex[:8]}-free", text="summarise this", identity=FREE)
    )

    assert source.fetch_count == 0
    assert queue.depth == 0
    media = (await session.execute(select(func.count()).select_from(MediaObject))).scalar_one()
    assert media == 0, "refused above the pipeline: not even a row is written"
    assert any(a.type == "SHOW_UPGRADE" for a in response.outbound_actions)


async def test_the_daily_media_allowance_refuses_before_the_fetch(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The per-student ceiling, enforced rather than merely counted.

    A page already read by vision has already been paid for, so the check has to
    happen before the fetch. It is projected - "would one more cross the line" -
    not measured after the fact.
    """
    from tutortwin.domain.budget import QuotaSnapshot

    service, queue, source = build(session_factory, tmp_path)
    pipeline = service._deps.media_pipeline  # noqa: SLF001 - asserting the wiring
    assert pipeline is not None

    # Already at the ceiling for today.
    spent = QuotaSnapshot(pdf_pages_today=100, daily_pdf_page_limit=100)
    assert spent.media_allowance_exceeded(pdf_pages=1) == "pdf_pages"

    from tutortwin.db.models import Subject

    subject = Subject(external_identity_type="test_phone", external_identity_value=PRO)
    session.add(subject)
    await session.flush()

    outcome = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=MediaRef(provider="test", media_id="notes.pdf", mime_type_hint="application/pdf"),
        message_type=MessageType.PDF,
        brief="summarise page 1",
        entitled=True,
        allowance=spent,
    )

    assert outcome.rejected
    assert outcome.reject_reason is RejectReason.DAILY_ALLOWANCE
    assert source.fetch_count == 0, "refused before a byte moved"
    assert queue.depth == 0
