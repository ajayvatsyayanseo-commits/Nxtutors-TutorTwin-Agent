"""Media, extraction-cache and job persistence.

State transitions go through `transition()` so the state machine is enforced at
the persistence boundary. A caller cannot move a row from WAITING_FOR_BRIEF
straight to FETCHED, whatever it intends.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.models import Job, MediaExtraction, MediaObject
from tutortwin.domain.events import MediaRef
from tutortwin.domain.media import (
    ExtractionMethod,
    MediaKind,
    MediaState,
    PageExtraction,
    RejectReason,
    assert_transition,
)


async def get_or_create_media(
    session: AsyncSession,
    *,
    subject_id: UUID,
    conversation_id: UUID | None,
    ref: MediaRef,
) -> MediaObject:
    """Idempotent on (source, source_media_id, subject).

    A redelivered event finds the existing row and its state, which is what stops
    a duplicate from starting a second pipeline.
    """
    existing = (
        await session.execute(
            select(MediaObject).where(
                MediaObject.source == ref.provider,
                MediaObject.source_media_id == ref.media_id,
                MediaObject.subject_id == subject_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = MediaObject(
        subject_id=subject_id,
        conversation_id=conversation_id,
        source=ref.provider,
        source_media_id=ref.media_id,
        state=str(MediaState.RECEIVED_REFERENCE),
        mime_type=ref.mime_type_hint,
        size_bytes=ref.size_hint,
    )
    session.add(row)
    await session.flush()
    return row


async def transition(
    session: AsyncSession,
    media: MediaObject,
    target: MediaState,
    *,
    reject_reason: RejectReason | None = None,
    **fields: Any,
) -> MediaObject:
    """Move to `target`, refusing any transition the machine forbids."""
    assert_transition(MediaState(media.state), target)
    media.state = str(target)
    if reject_reason is not None:
        media.reject_reason = str(reject_reason)
    for key, value in fields.items():
        setattr(media, key, value)
    await session.flush()
    return media


async def attach_brief(session: AsyncSession, media: MediaObject, brief: str) -> MediaObject:
    """Record the instruction that unlocks processing."""
    return await transition(session, media, MediaState.BRIEF_RECEIVED, brief=brief)


async def record_fetched(
    session: AsyncSession,
    media: MediaObject,
    *,
    blob_key: str,
    sha256: str,
    mime_type: str,
    size_bytes: int,
    kind: MediaKind,
    expires_at: datetime | None,
) -> MediaObject:
    return await transition(
        session,
        media,
        MediaState.FETCHED,
        blob_key=blob_key,
        sha256=sha256,
        mime_type=mime_type,
        size_bytes=size_bytes,
        kind=str(kind),
        expires_at=expires_at,
    )


# --- extraction cache ---------------------------------------------------------


async def load_cached_extraction(
    session: AsyncSession,
    *,
    subject_id: UUID,
    sha256: str,
    parser_version: str,
    pages: tuple[int, ...],
) -> dict[int, MediaExtraction]:
    """Owner-scoped cache read.

    Scoped by subject even though the content hash is global: identical bytes
    belonging to two students are two private documents, and serving one from
    the other's cache would be a cross-student leak.
    """
    if not pages:
        return {}
    rows = (
        await session.execute(
            select(MediaExtraction).where(
                MediaExtraction.subject_id == subject_id,
                MediaExtraction.sha256 == sha256,
                MediaExtraction.parser_version == parser_version,
                MediaExtraction.page_number.in_(pages),
            )
        )
    ).scalars()
    return {row.page_number: row for row in rows}


async def store_extraction(
    session: AsyncSession,
    *,
    subject_id: UUID,
    sha256: str,
    parser_version: str,
    ocr_version: str | None,
    page: PageExtraction,
) -> None:
    """Idempotent write; a re-extraction of the same page updates in place."""
    confidence = int(round(page.confidence * 10_000)) if page.confidence is not None else None
    await session.execute(
        pg_insert(MediaExtraction)
        .values(
            subject_id=subject_id,
            sha256=sha256,
            parser_version=parser_version,
            ocr_version=ocr_version,
            page_number=page.page_number,
            method=str(page.method),
            text=page.text,
            confidence=confidence,
        )
        .on_conflict_do_update(
            constraint="uq_media_extraction",
            set_={"text": page.text, "confidence": confidence},
        )
    )


def to_page_extraction(row: MediaExtraction) -> PageExtraction:
    return PageExtraction(
        page_number=row.page_number,
        method=ExtractionMethod(row.method),
        text=row.text,
        confidence=(row.confidence / 10_000) if row.confidence is not None else None,
    )


# --- jobs ---------------------------------------------------------------------


async def enqueue_job(
    session: AsyncSession,
    *,
    job_type: str,
    idempotency_key: str,
    owner_subject_id: UUID | None,
    media_object_id: UUID | None,
    correlation_id: str | None,
    payload: dict[str, Any] | None = None,
) -> tuple[Job | None, bool]:
    """Create a job unless one already exists for this key.

    Returns `(job, created)`. `created=False` means a duplicate event arrived and
    no second job was made - the property the duplicate-media test asserts.
    """
    result = await session.execute(
        pg_insert(Job)
        .values(
            job_type=job_type,
            state="PENDING",
            idempotency_key=idempotency_key,
            owner_subject_id=owner_subject_id,
            media_object_id=media_object_id,
            correlation_id=correlation_id,
            payload_json=payload or {},
        )
        .on_conflict_do_nothing(constraint="uq_job_idempotency")
        .returning(Job.id)
    )
    job_id = result.scalar_one_or_none()
    if job_id is None:
        existing = (
            await session.execute(select(Job).where(Job.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        return existing, False

    job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    return job, True


async def load_job(session: AsyncSession, job_id: UUID) -> Job | None:
    return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()


async def mark_job(
    session: AsyncSession,
    job: Job,
    *,
    state: str,
    error: str | None = None,
    next_retry_at: datetime | None = None,
) -> None:
    job.state = state
    if error is not None:
        job.last_error = error[:500]
    if next_retry_at is not None:
        job.next_retry_at = next_retry_at


async def load_extractions(session: AsyncSession, media: MediaObject) -> list[MediaExtraction]:
    """Every page of text read from one media object, in page order.

    Queried by owner and content hash rather than by media id, because that is
    how the table is keyed: the same file sent twice by the same student is one
    cached extraction serving both. Ordering matters - a worksheet answered with
    its pages shuffled is worse than one not answered at all.
    """
    if not media.sha256:
        return []
    rows = (
        await session.execute(
            select(MediaExtraction)
            .where(
                MediaExtraction.subject_id == media.subject_id,
                MediaExtraction.sha256 == media.sha256,
            )
            .order_by(MediaExtraction.page_number)
        )
    ).scalars()
    return list(rows)
