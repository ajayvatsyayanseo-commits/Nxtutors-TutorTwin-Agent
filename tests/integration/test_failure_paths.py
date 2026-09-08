"""Adversarial paths: dependency down, concurrent duplicates, blocked subject."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutortwin.api.app import create_app
from tutortwin.config import Settings
from tutortwin.db.engine import dispose_engine
from tutortwin.db.models import Conversation, Message
from tutortwin.domain.events import InboundMessage, MessageType, NormalizedEvent, SubjectRef
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


def make_event(message_id: str = "msg_race") -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"evt_{message_id}",
        request_id=f"req_{message_id}",
        correlation_id=f"corr_{message_id}",
        source="test_harness",
        subject=SubjectRef(external_type="test_phone", external_id=PRO),
        message=InboundMessage(message_id=message_id, type=MessageType.TEXT, text="hello"),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def build_service(session_factory) -> TutorTwinEntryService:
    return TutorTwinEntryService(
        EntryDependencies(
            identity=FakeIdentityGateway(),
            entitlement=FakeEntitlementGateway(plans={PRO: "PRO"}),
            tutor=FakeTutorGateway(),
            outbound=FakeOutboundGateway(),
            clock=FixedClock(),
            session_factory=session_factory,
            gateway_factory=None,
        )
    )


@pytest_asyncio.fixture
async def unreachable_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """App pointed at a database that is not listening."""
    await dispose_engine()
    settings = Settings(
        environment="test",
        # Port 1 is reserved and never accepting Postgres connections.
        database_url="postgresql+psycopg://postgres:postgres@127.0.0.1:1/nope",  # type: ignore[arg-type]
        db_connect_timeout_seconds=1,
        db_health_timeout_seconds=1.0,
    )
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
        app.router.lifespan_context(app),
    ):
        yield c
    await dispose_engine()


async def test_healthz_stays_up_when_database_is_down(
    unreachable_client: AsyncClient,
) -> None:
    """Liveness must not depend on Postgres, or a DB blip kills healthy containers."""
    response = await unreachable_client.get("/healthz")
    assert response.status_code == 200


async def test_readyz_reports_not_ready_when_database_is_down(
    unreachable_client: AsyncClient,
) -> None:
    response = await unreachable_client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] in {"error", "timeout"}


async def test_readyz_does_not_leak_dsn(unreachable_client: AsyncClient) -> None:
    response = await unreachable_client.get("/readyz")
    assert "postgres:postgres" not in response.text
    assert "127.0.0.1" not in response.text


async def test_concurrent_duplicates_execute_work_once(engine: object) -> None:
    """Two identical events racing must produce exactly one conversation.

    Each task needs its own session, because the unique-constraint conflict is
    resolved between transactions, not inside one.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)  # type: ignore[arg-type]
    service = build_service(factory)
    event = make_event("msg_concurrent")

    async def run() -> None:
        # The service owns its own transactions; a loser of the idempotency race
        # returns a replay rather than raising.
        await service.handle_event(event)

    await asyncio.gather(run(), run())

    async with factory() as s:
        conversations = await s.scalar(select(func.count()).select_from(Conversation))
        messages = await s.scalar(select(func.count()).select_from(Message))

    assert conversations == 1
    assert messages == 2
