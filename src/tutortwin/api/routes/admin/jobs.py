"""Job queue inspection, retry and cancel.

**Retry re-arms a row; it does not execute one.** The admin API never runs a job
inline — that would put media processing on a request thread and make an operator
click a request that can take minutes. Retry sets the state back to PENDING with
a fresh retry time, and the existing worker path picks it up.

**Cancel is refused for work already in flight.** A RUNNING job has a worker that
does not know it was cancelled, so marking it cancelled would produce a row that
disagrees with reality. The refusal explains itself rather than pretending.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from tutortwin.api.admin_deps import DbSession, require
from tutortwin.db.models import Job, MediaObject, Subject
from tutortwin.domain.admin import AdminActor, Permission
from tutortwin.domain.errors import ErrorCode, TutorTwinError
from tutortwin.observability.logging import get_logger
from tutortwin.repositories import admin as admin_repo

router = APIRouter()
logger = get_logger(__name__)

RETRYABLE_STATES = frozenset({"FAILED", "FAILED_PERMANENT", "PENDING"})
CANCELLABLE_STATES = frozenset({"PENDING", "FAILED"})


class JobView(BaseModel):
    id: str
    job_type: str
    state: str
    attempts: int
    max_attempts: int
    next_retry_at: datetime | None
    last_error: str | None
    correlation_id: str | None
    owner_subject_id: str | None
    owner_identity: str | None
    media_object_id: str | None
    media_state: str | None
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class JobPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[JobView]


def _view(job: Job, subject: Subject | None, media: MediaObject | None) -> JobView:
    return JobView(
        id=str(job.id),
        job_type=job.job_type,
        state=job.state,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        next_retry_at=job.next_retry_at,
        last_error=job.last_error,
        correlation_id=job.correlation_id,
        owner_subject_id=str(job.owner_subject_id) if job.owner_subject_id else None,
        owner_identity=subject.external_identity_value if subject else None,
        media_object_id=str(job.media_object_id) if job.media_object_id else None,
        media_state=media.state if media else None,
        payload=dict(job.payload_json),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get("/jobs", response_model=JobPage)
async def list_jobs(
    db: DbSession,
    state: str | None = Query(default=None, max_length=24),
    job_type: str | None = Query(default=None, max_length=48),
    correlation_id: str | None = Query(default=None, max_length=128),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=admin_repo.DEFAULT_PAGE_SIZE, ge=1, le=admin_repo.MAX_PAGE_SIZE),
    _: AdminActor = Depends(require(Permission.JOB_READ)),
) -> JobPage:
    base = (
        select(Job, Subject, MediaObject)
        .outerjoin(Subject, Subject.id == Job.owner_subject_id)
        .outerjoin(MediaObject, MediaObject.id == Job.media_object_id)
    )
    if state:
        base = base.where(Job.state == state)
    if job_type:
        base = base.where(Job.job_type == job_type)
    if correlation_id:
        base = base.where(Job.correlation_id == correlation_id)

    total = await admin_repo.count_rows(db, base)
    rows = (
        await db.execute(
            admin_repo.paginate(
                base.order_by(Job.updated_at.desc()), page=page, page_size=page_size
            )
        )
    ).all()

    return JobPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[_view(job, subject, media) for job, subject, media in rows],
    )


@router.get("/jobs/{job_id}", response_model=JobView)
async def job_detail(
    job_id: UUID,
    db: DbSession,
    _: AdminActor = Depends(require(Permission.JOB_READ)),
) -> JobView:
    row = (
        await db.execute(
            select(Job, Subject, MediaObject)
            .outerjoin(Subject, Subject.id == Job.owner_subject_id)
            .outerjoin(MediaObject, MediaObject.id == Job.media_object_id)
            .where(Job.id == job_id)
        )
    ).one_or_none()
    if row is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Job not found.")
    job, subject, media = row
    return _view(job, subject, media)


class JobActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


@router.post("/jobs/{job_id}/retry", response_model=JobView)
async def retry_job(
    job_id: UUID,
    payload: JobActionRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.JOB_WRITE)),
) -> JobView:
    """Re-arm a failed job. The attempt counter is not reset.

    Resetting it would let an operator loop a permanently-broken job forever;
    raising `max_attempts` instead is a visible, audited decision.
    """
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Job not found.")
    if job.state not in RETRYABLE_STATES:
        raise TutorTwinError(
            ErrorCode.CONFLICT,
            f"A job in state {job.state} cannot be retried.",
        )

    before = job.state
    job.state = "PENDING"
    job.next_retry_at = datetime.now(UTC)
    if job.attempts >= job.max_attempts:
        # An operator retrying an exhausted job means "give it one more", said
        # explicitly rather than by wiping the history of how often it failed.
        job.max_attempts = job.attempts + 1

    admin_repo.record_audit(
        db,
        actor=actor,
        action="JOB_RETRIED",
        target_type="job",
        target_id=str(job_id),
        reason=payload.reason,
        correlation_id=job.correlation_id,
        detail={"before_state": before, "attempts": job.attempts, "max_attempts": job.max_attempts},
    )
    # `updated_at` carries `onupdate=func.now()`, so SQLAlchemy expires it after
    # the UPDATE and re-fetches it on next access. That lazy fetch is synchronous
    # IO from an async context and raises MissingGreenlet rather than returning a
    # row - measured here before this line existed. `refresh` does the same read
    # as explicit, awaited IO.
    await db.flush()
    await db.refresh(job)
    view = _view(job, None, None)
    await db.commit()
    logger.info("job_retry_requested", job_id=str(job_id), actor=actor.admin_id)
    return view


@router.post("/jobs/{job_id}/cancel", response_model=JobView)
async def cancel_job(
    job_id: UUID,
    payload: JobActionRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.JOB_WRITE)),
) -> JobView:
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Job not found.")
    if job.state not in CANCELLABLE_STATES:
        raise TutorTwinError(
            ErrorCode.CONFLICT,
            f"A job in state {job.state} cannot be cancelled safely.",
        )

    before = job.state
    job.state = "CANCELLED"
    job.next_retry_at = None

    admin_repo.record_audit(
        db,
        actor=actor,
        action="JOB_CANCELLED",
        target_type="job",
        target_id=str(job_id),
        reason=payload.reason,
        correlation_id=job.correlation_id,
        detail={"before_state": before},
    )
    await db.flush()
    await db.refresh(job)
    view = _view(job, None, None)
    await db.commit()
    return view


__all__ = ["router"]
