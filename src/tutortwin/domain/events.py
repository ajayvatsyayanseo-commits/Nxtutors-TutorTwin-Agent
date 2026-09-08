"""Versioned normalized inbound event contract.

Every channel (test harness now, WhatsApp via Lead Intake in Phase 08) must
normalize into this shape. Business code never sees a channel-native payload.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EVENT_CONTRACT_VERSION = "1.0"


class MessageType(StrEnum):
    TEXT = "TEXT"
    AUDIO = "AUDIO"
    IMAGE = "IMAGE"
    PDF = "PDF"
    DOCUMENT = "DOCUMENT"
    ACTION = "ACTION"
    SYSTEM = "SYSTEM"


class OutboundActionType(StrEnum):
    SEND_TEXT = "SEND_TEXT"
    SEND_DOCUMENT = "SEND_DOCUMENT"
    SEND_IMAGE = "SEND_IMAGE"
    SEND_AUDIO = "SEND_AUDIO"
    SHOW_UPGRADE = "SHOW_UPGRADE"
    ASK_FILE_BRIEF = "ASK_FILE_BRIEF"
    SHOW_MENU = "SHOW_MENU"
    TUTOR_NOTIFICATION = "TUTOR_NOTIFICATION"


class RequestStatus(StrEnum):
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubjectRef(StrictModel):
    """Who sent this, in the source system's terms. Resolved by IdentityGateway."""

    external_type: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=256)


class MediaRef(StrictModel):
    """A pointer to media. Deliberately carries no bytes.

    The brief gate (Phase 03) holds this without any expensive processing.
    """

    provider: str = Field(min_length=1, max_length=64)
    media_id: str = Field(min_length=1, max_length=256)
    mime_type_hint: str | None = Field(default=None, max_length=255)
    size_hint: int | None = Field(default=None, ge=0)
    filename: str | None = Field(default=None, max_length=512)


class InboundMessage(StrictModel):
    message_id: str = Field(min_length=1, max_length=256)
    type: MessageType
    text: str | None = Field(default=None, max_length=16_000)
    media: MediaRef | None = None


class EventContext(StrictModel):
    locale: str = Field(default="en-IN", max_length=32)


class NormalizedEvent(StrictModel):
    """The single inbound contract. `source` + message id forms the idempotency key."""

    contract_version: Literal["1.0"] = "1.0"
    event_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=64)
    source_agent: str = Field(default="standalone", max_length=64)
    subject: SubjectRef
    message: InboundMessage
    context: EventContext = EventContext()
    occurred_at: datetime

    @property
    def idempotency_key(self) -> str:
        """Dedupe scope. message_id is the natural key; event_id is the fallback."""
        discriminator = self.message.message_id or self.event_id
        return f"{self.source}:{discriminator}"


class OutboundAction(StrictModel):
    type: OutboundActionType
    text: str | None = None


class UsageSummary(StrictModel):
    """Cost evidence. Phase 01 always reports zero paid calls."""

    paid_model_calls: int = 0


class EventResponse(StrictModel):
    conversation_id: str | None
    status: RequestStatus
    outbound_actions: tuple[OutboundAction, ...] = ()
    handoff: None = None
    usage: UsageSummary = UsageSummary()
    idempotent_replay: bool = False
