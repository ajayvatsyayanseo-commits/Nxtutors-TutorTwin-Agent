"""Media fetch sources and the task queue.

Phase 03 ships a local/test media source only. WhatsApp media fetching arrives
in Phase 08 with the Lead Intake bridge - adding it now would mean guessing at
an API this phase is forbidden to inspect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)


class MediaNotFound(KeyError):
    pass


class MediaSource(Protocol):
    """Fetches raw bytes for a provider-specific media id."""

    async def fetch(self, provider: str, media_id: str) -> bytes: ...


@dataclass(slots=True)
class InMemoryMediaSource:
    """Test source. Records every fetch so tests can assert download counts."""

    files: dict[str, bytes] = field(default_factory=dict)
    fetches: list[tuple[str, str]] = field(default_factory=list)

    def add(self, media_id: str, data: bytes) -> None:
        self.files[media_id] = data

    @property
    def fetch_count(self) -> int:
        return len(self.fetches)

    async def fetch(self, provider: str, media_id: str) -> bytes:
        self.fetches.append((provider, media_id))
        if media_id not in self.files:
            raise MediaNotFound(media_id)
        return self.files[media_id]


@dataclass(slots=True)
class LocalFileMediaSource:
    """Reads from a directory. For local development against real files."""

    root: Path
    fetches: list[tuple[str, str]] = field(default_factory=list)

    @property
    def fetch_count(self) -> int:
        return len(self.fetches)

    async def fetch(self, provider: str, media_id: str) -> bytes:
        self.fetches.append((provider, media_id))
        # media_id is provider-supplied; refuse anything that escapes the root.
        candidate = (self.root / media_id).resolve()
        if not str(candidate).startswith(str(self.root.resolve())):
            raise MediaNotFound(media_id)
        if not candidate.is_file():
            raise MediaNotFound(media_id)
        return candidate.read_bytes()


# --- task queue ---------------------------------------------------------------


class TaskQueue(Protocol):
    """Durable async work. The payload is only a job id - the worker reads all
    state from Postgres, so a retry cannot act on a stale snapshot."""

    async def enqueue(self, job_id: UUID, *, delay_seconds: int = 0) -> None: ...


@dataclass(slots=True)
class RecordingTaskQueue:
    """Test queue. Records enqueues; runs nothing."""

    enqueued: list[tuple[UUID, int]] = field(default_factory=list)

    @property
    def depth(self) -> int:
        return len(self.enqueued)

    async def enqueue(self, job_id: UUID, *, delay_seconds: int = 0) -> None:
        self.enqueued.append((job_id, delay_seconds))


@dataclass(slots=True)
class CloudTasksQueue:
    """Google Cloud Tasks, pushing to an OIDC-authenticated Cloud Run endpoint.

    No Celery, no Redis, no permanently running worker: Cloud Tasks holds the
    work and calls us, so the service still scales to zero.
    """

    project: str
    location: str
    queue: str
    target_url: str
    service_account_email: str
    _client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import tasks_v2

            self._client = tasks_v2.CloudTasksClient()
        return self._client

    async def enqueue(self, job_id: UUID, *, delay_seconds: int = 0) -> None:
        import asyncio
        import json

        from google.cloud import tasks_v2
        from google.protobuf import duration_pb2, timestamp_pb2

        client = self._get_client()
        parent = client.queue_path(self.project, self.location, self.queue)

        task: dict[str, object] = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": self.target_url,
                "headers": {"Content-Type": "application/json"},
                # IDs only. The worker loads durable state from Postgres.
                "body": json.dumps({"job_id": str(job_id)}).encode(),
                "oidc_token": {
                    "service_account_email": self.service_account_email,
                    "audience": self.target_url,
                },
            },
            "dispatch_deadline": duration_pb2.Duration(seconds=540),
        }
        if delay_seconds:
            import time

            schedule = timestamp_pb2.Timestamp()
            schedule.FromSeconds(int(time.time()) + delay_seconds)
            task["schedule_time"] = schedule

        await asyncio.to_thread(
            client.create_task,
            request={"parent": parent, "task": task},
        )
        logger.info("cloud_task_enqueued", job_id=str(job_id), delay_seconds=delay_seconds)


DISPATCH_ATTEMPTS = 4
"""Dispatch tries per job when the ceiling defers it. Four attempts at the
backoff below spans about a minute, which is the length of a media job - so a
deferred job is picked up as the fleet drains rather than waiting for a
restart."""

DISPATCH_BACKOFF_SECONDS = 8.0


@dataclass(slots=True)
class InProcessTaskQueue:
    """Dispatch on the same machine, for a deployment that owns its server.

    Cloud Tasks exists in this codebase because Cloud Run throttles a
    container's CPU between requests: work started after the response is written
    may simply never run, so it has to be handed to something that will call
    back in. **A normal always-on server has no such problem**, and on one the
    Cloud Tasks dependency buys nothing while costing a Google Cloud account, a
    queue, a service account and an OIDC round trip.

    This is not a polling worker. Nothing scans a table on a timer; the job is
    dispatched the moment it is created, exactly as Cloud Tasks would, by
    calling the same internal endpoint over the loopback interface. Using HTTP
    rather than an in-process function call is deliberate: it keeps ONE job
    execution path, so authentication, the retry policy, the concurrency ceiling
    and the idempotency guard cannot drift between deployments.

    Durability is unchanged and already handled: the job is a committed Postgres
    row with a state and an attempt count before this is ever called. A crash
    mid-flight leaves it retryable rather than lost - the difference from Cloud
    Tasks is that nothing re-delivers it automatically, so `services.job_sweep`
    runs at startup to pick up whatever the last process left behind.
    """

    base_url: str
    internal_key: str
    timeout_seconds: float = 600.0
    dispatched: list[UUID] = field(default_factory=list)

    async def enqueue(self, job_id: UUID, *, delay_seconds: int = 0) -> None:
        import asyncio

        self.dispatched.append(job_id)
        asyncio.create_task(self._run(job_id, delay_seconds))  # noqa: RUF006

    async def _run(self, job_id: UUID, delay_seconds: int, *, attempt: int = 1) -> None:
        import asyncio

        import httpx

        if delay_seconds:
            await asyncio.sleep(delay_seconds)

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}/internal/jobs/run",
                    json={"job_id": str(job_id)},
                    headers={"x-internal-key": self.internal_key},
                )
            logger.info(
                "in_process_job_dispatched", job_id=str(job_id), status=response.status_code
            )
            # 429 is the fleet concurrency ceiling saying "not now". Cloud Tasks
            # would re-deliver on its own backoff; nothing else here will, so
            # coming back is this queue's job. The startup sweep is the backstop
            # if the process dies before the last attempt.
            if response.status_code == 429 and attempt < DISPATCH_ATTEMPTS:
                await asyncio.sleep(DISPATCH_BACKOFF_SECONDS * attempt)
                await self._run(job_id, 0, attempt=attempt + 1)
        except Exception as exc:  # noqa: BLE001 - dispatch must never kill the caller
            # The row is committed and retryable; the startup sweep will find it.
            logger.error(
                "in_process_dispatch_failed",
                job_id=str(job_id),
                error_type=type(exc).__name__,
            )
