"""Jobs a restart left behind are picked up again.

On a server deployment `InProcessTaskQueue` dispatches once, in this process.
Cloud Tasks re-delivers on its own and needs none of this; nothing does it here,
so a job interrupted between commit and dispatch, or shed by the concurrency
ceiling, simply stopped - a student's photo accepted, charged against their
allowance, and never answered.

The docstring claimed a startup sweep for two phases before one existed. These
tests are what makes the claim true.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutortwin.db.models import Job, Subject
from tutortwin.repositories import media as media_repo
from tutortwin.services import job_sweep

pytestmark = pytest.mark.integration


class RecordingQueue:
    """Records what the sweep dispatched. Runs nothing."""

    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def enqueue(self, job_id: UUID, *, delay_seconds: int = 0) -> None:
        self.enqueued.append(job_id)


class BrokenQueue:
    def __init__(self) -> None:
        self.attempts = 0

    async def enqueue(self, job_id: UUID, *, delay_seconds: int = 0) -> None:
        self.attempts += 1
        raise RuntimeError("dispatch is down")


async def make_subject(session: AsyncSession) -> Subject:
    subject = Subject(
        external_identity_type="test_phone",
        external_identity_value=f"+91{uuid4().hex[:10]}",
    )
    session.add(subject)
    await session.flush()
    return subject


async def add_job(
    session: AsyncSession,
    subject: Subject,
    *,
    state: str,
    attempts: int = 0,
    max_attempts: int = 3,
    next_retry_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Job:
    job = Job(
        job_type="MEDIA_EXTRACT",
        state=state,
        idempotency_key=f"sweep:{uuid4()}",
        owner_subject_id=subject.id,
        attempts=attempts,
        max_attempts=max_attempts,
        next_retry_at=next_retry_at,
    )
    session.add(job)
    await session.flush()
    if updated_at is not None:
        # `updated_at` has an onupdate default, so it has to be forced past the
        # ORM to simulate a row nothing has touched in a quarter of an hour.
        await session.execute(
            Job.__table__.update().where(Job.id == job.id).values(updated_at=updated_at)
        )
    return job


async def test_every_stalled_shape_is_found(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    subject = await make_subject(session)

    pending = await add_job(session, subject, state="PENDING")
    retry_due = await add_job(
        session, subject, state="FAILED", attempts=1, next_retry_at=now - timedelta(minutes=5)
    )
    abandoned = await add_job(
        session, subject, state="RUNNING", attempts=1, updated_at=now - timedelta(minutes=40)
    )
    await session.commit()

    found = {job.id for job in await media_repo.find_resumable_jobs(session, now=now)}

    assert pending.id in found
    assert retry_due.id in found
    assert abandoned.id in found


async def test_settled_and_in_flight_jobs_are_left_alone(session: AsyncSession) -> None:
    """The sweep must not re-run finished work or fight the concurrency ceiling.

    A RUNNING row inside the in-flight window belongs to a live process, and is
    counted by the fleet ceiling - re-dispatching it would double-execute the
    job and understate how loaded the fleet is.
    """
    now = datetime.now(UTC)
    subject = await make_subject(session)

    succeeded = await add_job(session, subject, state="SUCCEEDED", attempts=1)
    permanent = await add_job(session, subject, state="FAILED_PERMANENT", attempts=3)
    exhausted = await add_job(
        session,
        subject,
        state="FAILED",
        attempts=3,
        max_attempts=3,
        next_retry_at=now - timedelta(hours=1),
    )
    in_flight = await add_job(
        session, subject, state="RUNNING", attempts=1, updated_at=now - timedelta(minutes=2)
    )
    not_yet_due = await add_job(
        session, subject, state="FAILED", attempts=1, next_retry_at=now + timedelta(minutes=10)
    )
    await session.commit()

    found = {job.id for job in await media_repo.find_resumable_jobs(session, now=now)}

    assert succeeded.id not in found
    assert permanent.id not in found
    assert exhausted.id not in found, "a job out of attempts must not be pushed around forever"
    assert in_flight.id not in found, "re-dispatching a live job would run it twice"
    assert not_yet_due.id not in found


async def test_the_sweep_dispatches_what_it_finds(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    subject = await make_subject(session)
    stalled = await add_job(session, subject, state="PENDING")
    await add_job(session, subject, state="SUCCEEDED", attempts=1)
    await session.commit()

    queue = RecordingQueue()
    dispatched = await job_sweep.sweep(session_factory, queue)  # type: ignore[arg-type]

    assert dispatched >= 1
    assert stalled.id in queue.enqueued


async def test_a_failing_dispatch_does_not_stop_startup(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A service that will not boot because it could not re-send an old photo
    is worse than one that boots and leaves it for the next restart."""
    subject = await make_subject(session)
    await add_job(session, subject, state="PENDING")
    await session.commit()

    queue = BrokenQueue()
    dispatched = await job_sweep.sweep(session_factory, queue)  # type: ignore[arg-type]

    assert dispatched == 0
    assert queue.attempts >= 1
