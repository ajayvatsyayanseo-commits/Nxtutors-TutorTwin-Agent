"""Pick up jobs that nothing is going to run.

Cloud Tasks re-delivers on its own, so a serverless deployment needs none of
this. A deployment that owns its server has no such safety net: `InProcessTaskQueue`
dispatches once, in this process, and anything that interrupts that dispatch
loses the work. Three ways that happens, all of them normal:

- the process restarts between committing the job row and dispatching it
- the process restarts while a job is RUNNING
- the job is shed by the fleet concurrency ceiling, which answers 429 and
  expects the caller's queue to come back later

The job row is durable in every case - it has a state, an attempt count and a
`next_retry_at` - so nothing is lost, only stalled. This is what un-stalls it,
at startup, which is the moment a restart has just created a batch of them.

`sweep` never raises. A service that refuses to start because it could not
re-dispatch an old photo is worse than one that starts and leaves it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutortwin.media.adapters import TaskQueue
from tutortwin.observability.logging import get_logger
from tutortwin.repositories import media as media_repo

logger = get_logger(__name__)

SWEEP_LIMIT = 200
"""Bounds a cold start. A backlog larger than this is re-swept on the next
restart rather than dispatched all at once into a service that is still warming
up - and a backlog that large is an incident, not a queue."""


async def sweep(
    session_factory: async_sessionmaker[AsyncSession],
    queue: TaskQueue,
    *,
    now: datetime | None = None,
) -> int:
    """Re-dispatch every stalled job. Returns how many were sent."""
    moment = now or datetime.now(UTC)
    try:
        async with session_factory() as session:
            stalled = await media_repo.find_resumable_jobs(session, now=moment, limit=SWEEP_LIMIT)
            # Read the ids out before leaving the session: the objects expire
            # with it, and the dispatch below is deliberately outside any
            # transaction because it makes a network call per job.
            job_ids = [(job.id, job.state, job.attempts) for job in stalled]
    except Exception as exc:
        logger.warning("job_sweep_query_failed", error_type=type(exc).__name__)
        return 0

    if not job_ids:
        logger.info("job_sweep_clean")
        return 0

    dispatched = 0
    for job_id, state, attempts in job_ids:
        try:
            await queue.enqueue(job_id)
        except Exception as exc:
            logger.warning(
                "job_sweep_dispatch_failed", job_id=str(job_id), error_type=type(exc).__name__
            )
            continue
        dispatched += 1
        logger.info("job_sweep_dispatched", job_id=str(job_id), was=state, attempts=attempts)

    logger.info("job_sweep_complete", found=len(job_ids), dispatched=dispatched)
    return dispatched


__all__ = ["SWEEP_LIMIT", "sweep"]
