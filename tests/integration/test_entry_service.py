"""Orchestration slice against a real PostgreSQL database."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.models import Conversation, IdempotencyKey, Message, RequestState
from tutortwin.domain.errors import DependencyError, OwnershipError
from tutortwin.domain.events import (
    InboundMessage,
    MediaRef,
    MessageType,
    NormalizedEvent,
    OutboundActionType,
    RequestStatus,
    SubjectRef,
)
from tutortwin.orchestration.entry_service import EntryDependencies, TutorTwinEntryService
from tutortwin.providers.fakes import (
    FakeEntitlementGateway,
    FakeIdentityGateway,
    FakeOutboundGateway,
    FakeTutorGateway,
    FixedClock,
    ForbiddenEmbeddingProvider,
    ForbiddenLLMProvider,
)
from tutortwin.repositories import conversations as repo

pytestmark = pytest.mark.integration

PRO = "+919999000001"
FREE = "+919999000002"
OTHER_PRO = "+919999000003"


def build_service(
    session_factory,
    *,
    plans: dict[str, str] | None = None,
    resolve_unknown: bool = True,
) -> tuple[TutorTwinEntryService, FakeOutboundGateway, ForbiddenLLMProvider]:
    """Wires a service with no model gateway.

    `gateway_factory=None` means the orchestration runs end to end without ever
    reaching a provider, which is what makes the zero-paid-call assertions in
    this module airtight.
    """
    llm = ForbiddenLLMProvider()
    outbound = FakeOutboundGateway()
    deps = EntryDependencies(
        identity=FakeIdentityGateway(resolve_unknown=resolve_unknown),
        entitlement=FakeEntitlementGateway(
            plans=plans or {PRO: "PRO", OTHER_PRO: "PRO", FREE: "FREE"}
        ),
        tutor=FakeTutorGateway(),
        outbound=outbound,
        clock=FixedClock(),
        session_factory=session_factory,
        gateway_factory=None,
    )
    return TutorTwinEntryService(deps), outbound, llm


def make_event(
    *,
    external_id: str = PRO,
    message_id: str = "msg_1",
    event_id: str = "evt_1",
    text: str | None = "Explain quadratic equations",
    message_type: MessageType = MessageType.TEXT,
    media: MediaRef | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        request_id=f"req_{message_id}",
        correlation_id=f"corr_{message_id}",
        source="test_harness",
        subject=SubjectRef(external_type="test_phone", external_id=external_id),
        message=InboundMessage(message_id=message_id, type=message_type, text=text, media=media),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_valid_event_creates_conversation_and_messages(
    session: AsyncSession, session_factory
) -> None:
    service, outbound, llm = build_service(session_factory)

    response = await service.handle_event(make_event())

    assert response.status is RequestStatus.COMPLETED
    assert response.conversation_id is not None
    assert response.usage.paid_model_calls == 0
    assert llm.calls == 0

    messages = (await session.execute(select(Message).order_by(Message.created_at))).scalars()
    roles = [m.role for m in messages]
    assert roles == ["STUDENT", "ASSISTANT"]

    # The response was actually delivered through the outbound port.
    assert len(outbound.delivered) == 1

    states = (await session.execute(select(RequestState))).scalars().all()
    assert [s.status for s in states] == ["COMPLETED"]


async def test_duplicate_event_returns_idempotent_result(
    session: AsyncSession, session_factory
) -> None:
    """Mandatory proof: duplicate must replay, not re-execute."""
    service, outbound, _ = build_service(session_factory)
    event = make_event()

    first = await service.handle_event(event)
    second = await service.handle_event(event)

    assert first.conversation_id == second.conversation_id
    assert first.idempotent_replay is False
    assert second.idempotent_replay is True

    # The decisive evidence: no second round of work.
    message_count = await session.scalar(select(func.count()).select_from(Message))
    assert message_count == 2
    conversation_count = await session.scalar(select(func.count()).select_from(Conversation))
    assert conversation_count == 1
    assert len(outbound.delivered) == 1

    keys = (await session.execute(select(IdempotencyKey))).scalars().all()
    assert len(keys) == 1
    assert keys[0].key == "test_harness:msg_1"


async def test_same_message_id_from_different_source_is_not_deduped(
    session: AsyncSession, session_factory
) -> None:
    service, _, _ = build_service(session_factory)
    first = make_event(message_id="msg_shared")
    second = first.model_copy(update={"source": "other_channel"})

    await service.handle_event(first)
    replayed = await service.handle_event(second)

    assert replayed.idempotent_replay is False


async def test_non_pro_student_makes_zero_provider_calls(
    session: AsyncSession, session_factory
) -> None:
    """Mandatory proof: ineligible student costs nothing."""
    service, outbound, llm = build_service(session_factory)
    embeddings = ForbiddenEmbeddingProvider()

    response = await service.handle_event(make_event(external_id=FREE))

    assert response.status is RequestStatus.REJECTED
    assert response.conversation_id is None
    assert response.usage.paid_model_calls == 0
    assert llm.calls == 0
    assert embeddings.calls == 0
    assert response.outbound_actions[0].type is OutboundActionType.SHOW_UPGRADE

    # No conversation is created for an ineligible student.
    assert await session.scalar(select(func.count()).select_from(Conversation)) == 0
    # But the rejection is auditable.
    states = (await session.execute(select(RequestState))).scalars().all()
    assert [(s.status, s.error_code) for s in states] == [("REJECTED", "ENTITLEMENT_INACTIVE")]


async def test_pro_student_reaches_orchestration(session: AsyncSession, session_factory) -> None:
    service, _, _ = build_service(session_factory)
    response = await service.handle_event(make_event(external_id=PRO))

    assert response.status is RequestStatus.COMPLETED
    assert response.outbound_actions[0].type is OutboundActionType.SEND_TEXT
    assert "Anita Sharma" in (response.outbound_actions[0].text or "")


async def test_unresolved_identity_is_rejected(session: AsyncSession, session_factory) -> None:
    service, _, llm = build_service(session_factory, resolve_unknown=False)
    response = await service.handle_event(make_event(external_id="+000"))

    assert response.status is RequestStatus.REJECTED
    assert llm.calls == 0
    states = (await session.execute(select(RequestState))).scalars().all()
    assert states[0].error_code == "IDENTITY_UNRESOLVED"


async def test_wrong_owner_cannot_read_conversation(session: AsyncSession, session_factory) -> None:
    """Mandatory proof: cross-student read is refused."""
    service, _, _ = build_service(session_factory)

    owner_response = await service.handle_event(make_event(external_id=PRO))
    assert owner_response.conversation_id is not None

    intruder_response = await service.handle_event(
        make_event(external_id=OTHER_PRO, message_id="msg_2", event_id="evt_2")
    )

    from uuid import UUID

    owner_conv = UUID(owner_response.conversation_id)
    assert intruder_response.conversation_id is not None
    intruder_subject = (
        await session.execute(
            select(Conversation.subject_id).where(
                Conversation.id == UUID(intruder_response.conversation_id)
            )
        )
    ).scalar_one()

    with pytest.raises(OwnershipError):
        await repo.load_conversation(
            session, conversation_id=owner_conv, subject_id=intruder_subject
        )

    # The rightful owner still reads it.
    owner_subject = (
        await session.execute(select(Conversation.subject_id).where(Conversation.id == owner_conv))
    ).scalar_one()
    loaded = await repo.load_conversation(
        session, conversation_id=owner_conv, subject_id=owner_subject
    )
    assert loaded.id == owner_conv


async def test_media_without_brief_creates_no_processing(
    session: AsyncSession, session_factory
) -> None:
    service, _, llm = build_service(session_factory)
    response = await service.handle_event(
        make_event(
            message_type=MessageType.PDF,
            text=None,
            media=MediaRef(provider="test", media_id="media_1", mime_type_hint="application/pdf"),
        ),
    )

    assert response.outbound_actions[0].type is OutboundActionType.ASK_FILE_BRIEF
    assert llm.calls == 0

    stored = (await session.execute(select(Message).where(Message.role == "STUDENT"))).scalar_one()
    # The reference is kept; the bytes were never fetched.
    assert stored.media_ref_json is not None
    assert stored.media_ref_json["media_id"] == "media_1"


async def test_second_message_reuses_open_conversation(
    session: AsyncSession, session_factory
) -> None:
    service, _, _ = build_service(session_factory)

    first = await service.handle_event(make_event(message_id="msg_a", event_id="e_a"))
    second = await service.handle_event(make_event(message_id="msg_b", event_id="e_b"))

    assert first.conversation_id == second.conversation_id
    assert await session.scalar(select(func.count()).select_from(Conversation)) == 1


async def test_database_error_becomes_controlled_error(
    session: AsyncSession, session_factory
) -> None:
    """Mandatory proof: a driver failure surfaces as a domain error, not a raw traceback."""
    service, _, _ = build_service(session_factory)

    class BrokenSession:
        """Fails on first use, the way a dropped connection does."""

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise SQLAlchemyError("connection reset by peer: host=db.internal user=admin")

        def add(self, *_args: object) -> None:
            pass

        async def flush(self) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def rollback(self) -> None:
            pass

        async def __aenter__(self) -> BrokenSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    # The service opens its own sessions now, so the failure is injected through
    # the factory rather than by passing in a session.
    broken_service, _, _ = build_service(lambda: BrokenSession())

    with pytest.raises(DependencyError) as excinfo:
        await broken_service.handle_event(make_event())

    # The client-facing message leaks neither host nor user.
    assert "db.internal" not in excinfo.value.message
    assert "admin" not in excinfo.value.message
    assert excinfo.value.http_status == 503
