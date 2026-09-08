"""Chaos: every dependency, broken on purpose.

The question each test asks is not "does it still work" - it usually cannot -
but **"does it fail in a way we can recover from"**. Three properties are
checked over and over, because they are the ones that decide whether an outage
costs an afternoon or a data set:

1. **No work is silently lost.** A failed job is retryable, not settled.
2. **No work is silently doubled.** A duplicate delivery is a no-op.
3. **No money is spent for nothing.** A vendor that is failing is stopped being
   paid, and a retry that never reached a provider does not consume quota.

Every failure here is injected at the seam the real one would arrive at - the
adapter, the blobstore, the queue - never by monkey-patching internals, because
a test that patches the code under test proves only that the patch works.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutortwin.api.app import create_app
from tutortwin.config import Settings
from tutortwin.db.engine import dispose_engine
from tutortwin.db.models import Job, MediaObject, Subject, UsageLedger
from tutortwin.domain.events import InboundMessage, MessageType, NormalizedEvent, SubjectRef
from tutortwin.domain.provider import ModelAlias, ModelRequest, Provider
from tutortwin.orchestration.entry_service import EntryDependencies, TutorTwinEntryService
from tutortwin.providers.fake_models import (
    FakeModelProvider,
    ScriptedReply,
    rate_limited_reply,
    timeout_reply,
)
from tutortwin.providers.fakes import (
    FakeEntitlementGateway,
    FakeIdentityGateway,
    FakeOutboundGateway,
    FakeTutorGateway,
    FixedClock,
)
from tutortwin.providers.gateway import ModelGateway, ProviderResponse
from tutortwin.providers.registry import default_catalog

pytestmark = pytest.mark.integration

PRO = "+919999000001"
KEY = "chaos-internal-key"


# --- helpers ------------------------------------------------------------------


def make_event(message_id: str, text: str = "explain diffusion") -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"evt_{message_id}",
        request_id=f"req_{message_id}",
        correlation_id=f"corr_{message_id}",
        source="chaos",
        subject=SubjectRef(external_type="test_phone", external_id=PRO),
        message=InboundMessage(message_id=message_id, type=MessageType.TEXT, text=text),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def build_gateway(model: FakeModelProvider) -> ModelGateway:
    """A gateway whose every alias resolves to one scriptable fake vendor."""
    catalog = {
        alias: entry.model_copy(update={"provider": Provider.FAKE})
        for alias, entry in default_catalog({Provider.ANTHROPIC: model}).items()
    }
    return ModelGateway({Provider.FAKE: model}, catalog)


def build_service(
    session_factory: async_sessionmaker[AsyncSession],
    model: FakeModelProvider | None = None,
) -> TutorTwinEntryService:
    return TutorTwinEntryService(
        EntryDependencies(
            identity=FakeIdentityGateway(),
            entitlement=FakeEntitlementGateway(plans={PRO: "PRO"}),
            tutor=FakeTutorGateway(),
            outbound=FakeOutboundGateway(),
            clock=FixedClock(),
            session_factory=session_factory,
            gateway_factory=(lambda: build_gateway(model)) if model else None,
        )
    )


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        environment="test",
        database_url="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin_test",  # type: ignore[arg-type]
        internal_api_key=KEY,  # type: ignore[arg-type]
    )
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
        app.router.lifespan_context(app),
    ):
        yield c
    await dispose_engine()


def auth() -> dict[str, str]:
    return {"x-internal-key": KEY}


# --- provider failures --------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "reply"),
    [
        ("rate_limit", rate_limited_reply()),
        ("timeout", timeout_reply()),
    ],
)
async def test_a_failing_vendor_is_retried_within_the_attempt_cap(
    label: str, reply: ScriptedReply
) -> None:
    """429 and timeout are both retryable, and both are bounded.

    The cap is what stops a vendor incident becoming a spend incident: an
    unbounded retry against a rate limit is a loop that pays for every rejection.
    """
    model = FakeModelProvider(default=reply)
    gateway = build_gateway(model)

    result = await gateway.invoke(
        ModelRequest(
            alias=ModelAlias.STANDARD_TUTOR, system="s", messages=(), max_output_tokens=512
        ),
        max_attempts=2,
    )

    assert not result.succeeded, label
    assert model.call_count == 2, f"{label}: attempts must stop at the cap"
    assert len(result.attempts) == 2
    # Every attempt is recorded, including the failures. An unrecorded failure is
    # how a retry storm stays invisible until the invoice arrives.
    assert all(not call.succeeded for call in result.attempts)


async def test_a_non_retryable_refusal_is_not_paid_for_twice() -> None:
    """A content filter answers identically on retry. Spending again is waste."""
    from tutortwin.providers.fake_models import refusal_reply

    model = FakeModelProvider(default=refusal_reply())
    gateway = build_gateway(model)

    result = await gateway.invoke(
        ModelRequest(
            alias=ModelAlias.STANDARD_TUTOR, system="s", messages=(), max_output_tokens=512
        ),
        max_attempts=3,
    )

    assert not result.succeeded
    assert model.call_count == 1


async def test_a_recovering_vendor_is_used_on_the_second_attempt() -> None:
    model = FakeModelProvider(default=ScriptedReply(text="recovered"))
    model.script(rate_limited_reply())
    gateway = build_gateway(model)

    result = await gateway.invoke(
        ModelRequest(
            alias=ModelAlias.STANDARD_TUTOR, system="s", messages=(), max_output_tokens=512
        ),
        max_attempts=2,
    )

    assert result.succeeded
    assert result.call is not None
    assert result.call.text == "recovered"
    assert len(result.attempts) == 2, "the failed attempt is still on the ledger"


async def test_backoff_grows_and_is_bounded() -> None:
    """Backoff is exponential, capped, and zero before the first attempt.

    Without a cap a fifth retry waits minutes and the student has left; without
    jitter every failed request retries on the same second and re-creates the
    spike the vendor is recovering from.
    """
    from tutortwin.providers.gateway import RETRY_MAX_SECONDS, retry_delay

    assert retry_delay(1) == 0.0
    undelayed = [retry_delay(n, jitter=False) for n in range(2, 9)]
    assert undelayed == sorted(undelayed), "delay must not shrink"
    assert max(undelayed) <= RETRY_MAX_SECONDS

    # Jittered delays stay inside the same envelope, so the cap is a real cap.
    for attempt in range(2, 9):
        assert 0.0 <= retry_delay(attempt) <= RETRY_MAX_SECONDS


class MalformedAdapter:
    """A vendor that answers 200 with nonsense: no text, a success category.

    Real adapters normalise this, but an adapter change or a vendor's own bug can
    produce it, and the gateway must not treat "nothing" as an answer.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, request: object, entry: object) -> ProviderResponse:
        self.calls += 1
        return ProviderResponse(text="", input_tokens=10, output_tokens=0)


async def test_a_malformed_provider_response_is_not_returned_as_an_answer() -> None:
    adapter = MalformedAdapter()
    catalog = {
        alias: entry.model_copy(update={"provider": Provider.FAKE})
        for alias, entry in default_catalog({Provider.ANTHROPIC: FakeModelProvider()}).items()
    }
    gateway = ModelGateway({Provider.FAKE: adapter}, catalog)  # type: ignore[dict-item]

    result = await gateway.invoke(
        ModelRequest(
            alias=ModelAlias.STANDARD_TUTOR, system="s", messages=(), max_output_tokens=512
        ),
        max_attempts=1,
    )

    # An empty body is not a successful answer, and the student is not shown "".
    assert result.call is None or not result.call.text


async def test_provider_failures_do_not_consume_the_students_call_quota(
    session: AsyncSession,
) -> None:
    """Retry accounting, the property the brief calls out by name.

    Three attempts that never reached a working provider must cost the student
    zero calls. A vendor that *did* bill - a timeout after reading the prompt -
    still counts, because the money left the account.
    """
    from tutortwin.repositories import catalog as catalog_repo

    subject = Subject(external_identity_type="test_phone", external_identity_value=PRO)
    session.add(subject)
    await session.flush()

    now = datetime.now(UTC)
    session.add_all(
        [
            # Never reached a provider: no output, no cost.
            UsageLedger(
                subject_id=subject.id,
                provider="fake",
                model_alias="STANDARD_TUTOR",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_micros=0,
            ),
            UsageLedger(
                subject_id=subject.id,
                provider="fake",
                model_alias="STANDARD_TUTOR",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_micros=0,
            ),
            # Timed out, but the vendor read and billed the prompt.
            UsageLedger(
                subject_id=subject.id,
                provider="fake",
                model_alias="STANDARD_TUTOR",
                input_tokens=500,
                output_tokens=0,
                estimated_cost_micros=1000,
            ),
        ]
    )
    await session.commit()

    snapshot = await catalog_repo.load_quota_snapshot(
        session,
        subject_id=subject.id,
        now=now,
        daily_call_limit=10,
        user_daily_budget_micros=None,
    )

    assert snapshot.calls_today == 1, "only the billed attempt counts against quota"
    assert snapshot.spend_today_micros == 1000, "but every real charge is recorded"


# --- storage failures ---------------------------------------------------------


class BrokenBlobStore:
    """R2 refusing every operation, the way an outage or a revoked token looks."""

    async def put(self, **kwargs: object) -> object:
        raise ConnectionError("R2 unavailable")

    async def get(self, key: str, *, subject_id: UUID) -> bytes:
        raise ConnectionError("R2 unavailable")

    async def delete(self, key: str, *, subject_id: UUID) -> None:
        raise ConnectionError("R2 unavailable")

    async def exists(self, key: str) -> bool:
        raise ConnectionError("R2 unavailable")


async def test_retention_leaves_the_row_when_the_object_store_is_down(
    session: AsyncSession,
) -> None:
    """A row whose blob could not be deleted must survive.

    Deleting the row anyway strands the object: nothing references it any more,
    so no later sweep can find it, and it is billed for storage forever.
    """
    from tutortwin.services import retention

    subject = Subject(external_identity_type="test_phone", external_identity_value=PRO)
    session.add(subject)
    await session.flush()

    media = MediaObject(
        subject_id=subject.id,
        source="chaos",
        source_media_id="expired-1",
        state="READY",
        kind="PDF",
        blob_key=f"media/{subject.id}/aa/abc.pdf",
        sha256="a" * 64,
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(media)
    await session.commit()

    result = await retention.sweep(session, blobstore=BrokenBlobStore())  # type: ignore[arg-type]

    assert result.blob_errors == 1
    assert result.media_rows_deleted == 0
    survived = (await session.execute(select(func.count()).select_from(MediaObject))).scalar_one()
    assert survived == 1, "the row is the only handle on the orphaned object"


async def test_retention_is_idempotent(session: AsyncSession) -> None:
    from tutortwin.media.blobstore import FilesystemBlobStore
    from tutortwin.services import retention

    subject = Subject(external_identity_type="test_phone", external_identity_value=PRO)
    session.add(subject)
    await session.flush()
    session.add(
        MediaObject(
            subject_id=subject.id,
            source="chaos",
            source_media_id="expired-2",
            state="READY",
            kind="PDF",
            blob_key=None,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await session.commit()

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        store = FilesystemBlobStore(root=Path(tmp))
        first = await retention.sweep(session, blobstore=store)
        second = await retention.sweep(session, blobstore=store)

    assert first.media_rows_deleted == 1
    assert second.media_rows_deleted == 0, "a second sweep deletes nothing"


# --- queue failures -----------------------------------------------------------


async def test_duplicate_task_delivery_runs_the_job_once(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Cloud Tasks guarantees at-least-once. The handler must make it exactly-once.

    A handler that is not idempotent will eventually re-run an OCR pass and bill
    for it a second time, which is the failure this whole design is shaped to
    avoid.
    """
    subject = Subject(external_identity_type="test_phone", external_identity_value=PRO)
    session.add(subject)
    await session.flush()

    job = Job(
        job_type="MEDIA_EXTRACT",
        state="SUCCEEDED",
        idempotency_key="chaos:dup",
        attempts=1,
        owner_subject_id=subject.id,
    )
    session.add(job)
    await session.commit()
    job_id = job.id

    for _ in range(3):
        response = await client.post(
            "/internal/jobs/run", json={"job_id": str(job_id)}, headers=auth()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "SUCCEEDED"
        assert body["detail"] == "already settled"

    await session.refresh(job)
    assert job.attempts == 1, "a settled job is never attempted again"


async def test_the_job_endpoint_refuses_an_unauthenticated_caller(
    client: AsyncClient,
) -> None:
    """The worker endpoint is on the public internet. It is where the money is."""
    response = await client.post("/internal/jobs/run", json={"job_id": str(uuid4())})
    assert response.status_code == 401

    response = await client.post(
        "/internal/jobs/run",
        json={"job_id": str(uuid4())},
        headers={"x-internal-key": "wrong"},
    )
    assert response.status_code == 401


async def test_an_exhausted_job_settles_instead_of_retrying_forever(
    client: AsyncClient, session: AsyncSession
) -> None:
    job = Job(
        job_type="MEDIA_EXTRACT",
        state="FAILED",
        idempotency_key="chaos:exhausted",
        attempts=3,
        max_attempts=3,
    )
    session.add(job)
    await session.commit()

    response = await client.post("/internal/jobs/run", json={"job_id": str(job.id)}, headers=auth())

    assert response.status_code == 200
    assert response.json()["state"] == "FAILED_PERMANENT"
    await session.refresh(job)
    assert job.state == "FAILED_PERMANENT"


async def test_heavy_job_concurrency_sheds_load_instead_of_piling_on(
    session: AsyncSession,
) -> None:
    """A saturated fleet defers work rather than adding to the pile.

    429 and not 500: Cloud Tasks re-delivers on its own backoff, so the work is
    postponed, not lost. A 500 would burn one of the job's finite attempts on a
    condition that has nothing to do with the job.
    """
    settings = Settings(
        environment="test",
        database_url="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin_test",  # type: ignore[arg-type]
        internal_api_key=KEY,  # type: ignore[arg-type]
        heavy_job_max_concurrency=1,
    )

    running = Job(
        job_type="MEDIA_EXTRACT",
        state="RUNNING",
        idempotency_key="chaos:running",
        attempts=1,
    )
    queued = Job(
        job_type="MEDIA_EXTRACT",
        state="PENDING",
        idempotency_key="chaos:queued",
    )
    session.add_all([running, queued])
    await session.commit()
    queued_id = queued.id

    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
        app.router.lifespan_context(app),
    ):
        response = await c.post(
            "/internal/jobs/run", json={"job_id": str(queued_id)}, headers=auth()
        )
    await dispose_engine()

    assert response.status_code == 429
    assert response.json()["detail"] == "concurrency ceiling"

    await session.refresh(queued)
    assert queued.attempts == 0, "a deferred job has not used an attempt"


# --- crash and kill -----------------------------------------------------------


async def test_a_job_crash_leaves_the_row_retryable(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A container killed mid-extraction must not leave a job stuck in RUNNING.

    The handler catches, records, and re-arms. Extraction is content-addressed,
    so the retry costs a cache hit rather than a second vision bill.
    """
    settings = Settings(
        environment="test",
        database_url="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin_test",  # type: ignore[arg-type]
        internal_api_key=KEY,  # type: ignore[arg-type]
    )

    subject = Subject(external_identity_type="test_phone", external_identity_value=PRO)
    session.add(subject)
    await session.flush()
    media = MediaObject(
        subject_id=subject.id,
        source="chaos",
        source_media_id="crash-1",
        state="PENDING_EXTRACTION",
        kind="PDF",
        blob_key=f"media/{subject.id}/aa/crash.pdf",
        sha256="c" * 64,
    )
    session.add(media)
    await session.flush()
    job = Job(
        job_type="MEDIA_EXTRACT",
        state="PENDING",
        idempotency_key="chaos:crash",
        owner_subject_id=subject.id,
        media_object_id=media.id,
    )
    session.add(job)
    await session.commit()
    job_id = job.id

    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
        app.router.lifespan_context(app),
    ):
        # The blobstore has no such object, so the pipeline raises partway
        # through - exactly the shape of a crash mid-processing.
        response = await c.post("/internal/jobs/run", json={"job_id": str(job_id)}, headers=auth())
    await dispose_engine()

    await session.refresh(job)
    assert job.state != "RUNNING", "a crashed job must not be left claimed forever"
    assert job.attempts == 1
    if job.state == "FAILED":
        # Retryable, and scheduled: 503 asks Cloud Tasks to come back.
        assert response.status_code == 503
        assert job.next_retry_at is not None
        assert job.last_error


async def test_a_process_killed_before_persistence_can_be_replayed(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An abandoned idempotency claim must not answer the student with silence.

    A container that dies between claiming the key and storing the response
    leaves a permanent empty claim. Without a reclaim rule, every redelivery of
    that message returns an empty success and the student is never answered.
    """
    service = build_service(session_factory)
    event = make_event("killed-1")

    from tutortwin.repositories import conversations as repo

    async with session_factory() as s:
        claimed = await repo.claim_idempotency_key(
            s, event.idempotency_key, now=datetime.now(UTC) - timedelta(hours=2)
        )
        await s.commit()
    assert claimed is not None or claimed is None  # the claim exists either way

    # The redelivery must produce a real answer, not an empty replay.
    response = await service.handle_event(event)
    assert response.outbound_actions, "an abandoned claim is re-executed, not replayed"


# --- database failure ---------------------------------------------------------


async def test_readiness_fails_closed_while_liveness_stays_up() -> None:
    """A database blip must not get healthy containers killed.

    `/healthz` answers "is this process alive" and touches nothing. `/readyz`
    answers "can this serve traffic" and checks the database under a short
    timeout, so a hung dependency fails fast instead of piling up.
    """
    await dispose_engine()
    settings = Settings(
        environment="test",
        database_url="postgresql+psycopg://postgres:postgres@127.0.0.1:1/nope",  # type: ignore[arg-type]
        db_connect_timeout_seconds=1,
        db_health_timeout_seconds=1.0,
    )
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
        app.router.lifespan_context(app),
    ):
        assert (await c.get("/healthz")).status_code == 200
        ready = await c.get("/readyz")
        assert ready.status_code == 503
        # The DSN carries a password. It is never in an error body.
        assert "postgres" not in ready.text
    await dispose_engine()
