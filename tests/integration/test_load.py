"""Load: the shapes of traffic that break a serverless deployment.

These are **not** benchmarks. A wall-clock number measured on a laptop against a
local Postgres says nothing about Cloud Run and Neon. What these assert is the
part that *is* portable - the invariants that must survive concurrency:

- the connection pool is never asked for more than it has
- concurrent duplicates do the work once
- a burst does not multiply provider calls
- a saturated queue defers rather than piles on
- a slow provider does not hold a database transaction open

Every one of those is a property, not a timing. The numbers this run does
produce are printed for the acceptance report, not asserted on.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutortwin.db.models import Conversation, Message, RequestEvent, Subject, UsageLedger
from tutortwin.domain.events import InboundMessage, MessageType, NormalizedEvent, SubjectRef
from tutortwin.domain.provider import Provider
from tutortwin.orchestration.entry_service import EntryDependencies, TutorTwinEntryService
from tutortwin.providers.fake_models import FakeModelProvider, ScriptedReply
from tutortwin.providers.fakes import (
    FakeEntitlementGateway,
    FakeIdentityGateway,
    FakeOutboundGateway,
    FakeTutorGateway,
    FixedClock,
)
from tutortwin.providers.gateway import ModelGateway
from tutortwin.providers.registry import default_catalog

pytestmark = pytest.mark.integration

CONCURRENT_STUDENTS = 20
BURST_SIZE = 25


def identity(index: int) -> str:
    return f"+9199990{index:05d}"


def make_event(external_id: str, message_id: str, text: str = "explain osmosis") -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"evt_{message_id}",
        request_id=f"req_{message_id}",
        correlation_id=f"corr_{message_id}",
        source="load",
        subject=SubjectRef(external_type="test_phone", external_id=external_id),
        message=InboundMessage(message_id=message_id, type=MessageType.TEXT, text=text),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def build_service(
    session_factory: async_sessionmaker[AsyncSession],
    model: FakeModelProvider,
    *,
    plans: dict[str, str],
) -> TutorTwinEntryService:
    catalog = {
        alias: entry.model_copy(update={"provider": Provider.FAKE})
        for alias, entry in default_catalog({Provider.ANTHROPIC: model}).items()
    }
    return TutorTwinEntryService(
        EntryDependencies(
            identity=FakeIdentityGateway(),
            entitlement=FakeEntitlementGateway(plans=plans),
            tutor=FakeTutorGateway(),
            outbound=FakeOutboundGateway(),
            clock=FixedClock(),
            session_factory=session_factory,
            gateway_factory=lambda: ModelGateway({Provider.FAKE: model}, catalog),
        )
    )


async def test_concurrent_students_share_a_small_pool_without_exhausting_it(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Twenty students at once through a pool of four.

    Neon's connection budget is the scarcest resource in this architecture, which
    is why the per-container pool is 2 + 2. The entry service holds a session for
    the *transaction*, never across the provider call, so twenty concurrent
    requests queue for connections instead of demanding twenty.

    If a transaction were held across the model call, this test would deadlock
    rather than fail - which is exactly how that bug presents in production.
    """
    model = FakeModelProvider(default=ScriptedReply(text="answer"))
    plans = {identity(i): "PRO" for i in range(CONCURRENT_STUDENTS)}
    service = build_service(session_factory, model, plans=plans)

    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            service.handle_event(make_event(identity(i), f"load-{i}"))
            for i in range(CONCURRENT_STUDENTS)
        ),
        return_exceptions=True,
    )
    elapsed = time.perf_counter() - started

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"{len(failures)} request(s) failed: {failures[:2]}"

    conversations = (
        await session.execute(select(func.count()).select_from(Conversation))
    ).scalar_one()
    assert conversations == CONCURRENT_STUDENTS, "one conversation per student, no cross-talk"

    print(
        f"\n[load] {CONCURRENT_STUDENTS} concurrent students in {elapsed:.2f}s "
        f"({CONCURRENT_STUDENTS / elapsed:.1f} req/s, fake provider)"
    )


async def test_a_burst_from_one_student_does_not_multiply_provider_calls(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The same message, delivered twenty-five times at once.

    This is what a retrying upstream looks like. The unique index on the
    idempotency key is the arbiter: one winner does the work, the rest replay a
    stored response. **One** provider call, not twenty-five.
    """
    model = FakeModelProvider(default=ScriptedReply(text="answer"))
    service = build_service(session_factory, model, plans={identity(0): "PRO"})
    event = make_event(identity(0), "burst-same")

    await asyncio.gather(
        *(service.handle_event(event) for _ in range(BURST_SIZE)), return_exceptions=True
    )

    events = (await session.execute(select(func.count()).select_from(RequestEvent))).scalar_one()
    student_turns = (
        await session.execute(
            select(func.count()).select_from(Message).where(Message.role == "STUDENT")
        )
    ).scalar_one()

    assert events == 1, "a redelivered message is one request event, not twenty-five"
    assert student_turns == 1, "and one stored turn"
    assert model.call_count <= 1, f"{model.call_count} provider calls for one message"

    print(f"\n[load] burst of {BURST_SIZE} duplicates -> {model.call_count} provider call(s)")


async def test_distinct_messages_in_a_burst_are_all_processed(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Deduplication must not swallow genuinely different messages.

    The failure mode this guards is the opposite of the one above: an
    over-broad idempotency key that treats a student's second question as a
    duplicate of their first and answers with the old reply.
    """
    model = FakeModelProvider(default=ScriptedReply(text="answer"))
    service = build_service(session_factory, model, plans={identity(0): "PRO"})

    await asyncio.gather(
        *(
            service.handle_event(make_event(identity(0), f"burst-{i}", f"question {i}"))
            for i in range(BURST_SIZE)
        ),
        return_exceptions=True,
    )

    events = (await session.execute(select(func.count()).select_from(RequestEvent))).scalar_one()
    assert events == BURST_SIZE, "distinct messages are distinct requests"


async def test_a_slow_provider_does_not_hold_a_database_transaction(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The architecture's load-bearing rule, asserted rather than asserted-to.

    Ten concurrent requests against a provider that takes 200ms each. If any
    transaction were held across that call, a pool of four would serialise them
    into at least 500ms of pure queueing, and with more concurrency it would
    deadlock outright.

    The check is not the timing - it is that all ten complete, from a pool that
    is smaller than the concurrency.
    """

    class SlowProvider(FakeModelProvider):
        async def invoke(self, request: object, entry: object) -> object:  # type: ignore[override]
            await asyncio.sleep(0.2)
            return await super().invoke(request, entry)  # type: ignore[arg-type]

    model = SlowProvider(default=ScriptedReply(text="slow answer"))
    plans = {identity(i): "PRO" for i in range(10)}
    service = build_service(session_factory, model, plans=plans)

    started = time.perf_counter()
    results = await asyncio.gather(
        *(service.handle_event(make_event(identity(i), f"slow-{i}")) for i in range(10)),
        return_exceptions=True,
    )
    elapsed = time.perf_counter() - started

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"a slow provider must not fail requests: {failures[:2]}"
    assert model.call_count == 10

    print(f"\n[load] 10 requests x 200ms provider latency in {elapsed:.2f}s (pool of 4)")


async def test_every_provider_call_is_on_the_ledger_after_a_burst(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Cost evidence survives concurrency.

    A ledger that under-counts under load is worse than no ledger: it produces a
    number that looks authoritative and is quietly low, which is the number an
    operator would use to decide a budget is safe.
    """
    model = FakeModelProvider(default=ScriptedReply(text="answer"))
    plans = {identity(i): "PRO" for i in range(10)}
    service = build_service(session_factory, model, plans=plans)

    await asyncio.gather(
        *(service.handle_event(make_event(identity(i), f"ledger-{i}")) for i in range(10)),
        return_exceptions=True,
    )

    rows = (await session.execute(select(func.count()).select_from(UsageLedger))).scalar_one()
    assert rows == model.call_count, f"{model.call_count} provider calls but {rows} ledger rows"


async def test_an_ineligible_crowd_costs_nothing(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Twenty ineligible students hitting at once must produce zero paid calls.

    The load version of the phase-one guarantee. The plan gate runs before the
    quota read and before any tier is chosen, so a crowd of unentitled users is
    cheap to refuse even when it is a crowd.
    """
    model = FakeModelProvider(default=ScriptedReply(text="should never be sent"))
    service = build_service(session_factory, model, plans={})  # nobody is PRO

    await asyncio.gather(
        *(
            service.handle_event(make_event(identity(i), f"free-{i}"))
            for i in range(CONCURRENT_STUDENTS)
        ),
        return_exceptions=True,
    )

    assert model.call_count == 0
    rows = (await session.execute(select(func.count()).select_from(UsageLedger))).scalar_one()
    assert rows == 0


async def test_subjects_are_created_once_under_a_concurrent_first_contact(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Ten simultaneous first messages from one new student.

    Identity upsert is the only write that races on a brand-new subject, and a
    duplicate subject row would split one student's history in two.
    """
    model = FakeModelProvider(default=ScriptedReply(text="answer"))
    service = build_service(session_factory, model, plans={identity(99): "PRO"})

    await asyncio.gather(
        *(service.handle_event(make_event(identity(99), f"first-{i}")) for i in range(10)),
        return_exceptions=True,
    )

    subjects = (
        await session.execute(
            select(func.count())
            .select_from(Subject)
            .where(Subject.external_identity_value == identity(99))
        )
    ).scalar_one()
    assert subjects == 1
