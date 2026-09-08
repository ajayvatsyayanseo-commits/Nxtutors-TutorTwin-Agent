"""Normalized event contract and the deterministic capability."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tutortwin.capabilities.placeholder import BRIEF_PROMPT, build_placeholder_reply, has_brief
from tutortwin.domain.events import (
    EventContext,
    InboundMessage,
    MediaRef,
    MessageType,
    NormalizedEvent,
    OutboundActionType,
    SubjectRef,
)
from tutortwin.domain.models import TutorPersona, TutorProfile
from tutortwin.providers.fakes import deterministic_uuid


def make_event(
    *,
    message_type: MessageType = MessageType.TEXT,
    text: str | None = "Explain quadratic equations",
    media: MediaRef | None = None,
    message_id: str = "msg_1",
    source: str = "test_harness",
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id="evt_1",
        request_id="req_1",
        correlation_id="corr_1",
        source=source,
        subject=SubjectRef(external_type="test_phone", external_id="+919999999999"),
        message=InboundMessage(message_id=message_id, type=message_type, text=text, media=media),
        context=EventContext(),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_idempotency_key_uses_source_and_message_id() -> None:
    event = make_event()
    assert event.idempotency_key == "test_harness:msg_1"


def test_idempotency_key_is_scoped_by_source() -> None:
    """The same message id from two channels must not collide."""
    a = make_event(source="test_harness")
    b = make_event(source="lead_intake")
    assert a.idempotency_key != b.idempotency_key


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate(
            {
                "event_id": "evt_1",
                "request_id": "req_1",
                "correlation_id": "corr_1",
                "source": "test_harness",
                "subject": {"external_type": "test_phone", "external_id": "+91"},
                "message": {"message_id": "m", "type": "TEXT", "text": "hi"},
                "occurred_at": "2026-01-01T00:00:00Z",
                "unexpected": "value",
            }
        )


def test_empty_external_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SubjectRef(external_type="test_phone", external_id="")


def test_overlong_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InboundMessage(message_id="m", type=MessageType.TEXT, text="x" * 16_001)


def test_media_without_brief_asks_for_brief() -> None:
    """Cost gate: an unexplained attachment must not trigger processing."""
    event = make_event(
        message_type=MessageType.PDF,
        text=None,
        media=MediaRef(provider="test", media_id="media_1", mime_type_hint="application/pdf"),
    )
    actions = build_placeholder_reply(event, None)
    assert len(actions) == 1
    assert actions[0].type is OutboundActionType.ASK_FILE_BRIEF
    assert actions[0].text == BRIEF_PROMPT


def test_media_with_brief_proceeds() -> None:
    event = make_event(
        message_type=MessageType.IMAGE,
        text="solve question 4",
        media=MediaRef(provider="test", media_id="media_2"),
    )
    actions = build_placeholder_reply(event, None)
    assert actions[0].type is OutboundActionType.SEND_TEXT


@pytest.mark.parametrize("text", [None, "", "  ", "ok"])
def test_has_brief_boundary(text: str | None) -> None:
    event = make_event(message_type=MessageType.PDF, text=text)
    assert has_brief(event) is (text is not None and len(text.strip()) >= 3)


def test_assistant_identity_never_impersonates_tutor() -> None:
    tutor = TutorProfile(
        id=deterministic_uuid("tutor", "Anita Sharma"),
        display_name="Anita Sharma",
        persona=TutorPersona(version=1),
    )
    event = make_event()
    actions = build_placeholder_reply(event, tutor)
    assert actions[0].text is not None
    assert actions[0].text.startswith("TutorTwin - AI Assistant for Anita Sharma")


def test_deterministic_uuid_is_stable() -> None:
    assert deterministic_uuid("subject", "test_phone", "+91") == deterministic_uuid(
        "subject", "test_phone", "+91"
    )
