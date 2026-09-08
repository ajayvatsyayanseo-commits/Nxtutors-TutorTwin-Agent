"""SQLAlchemy 2 ORM models - Phase 01 tables only.

UUID strategy: application-generated uuid4 primary keys everywhere. Generating in
Python (not DEFAULT gen_random_uuid()) keeps the ID available before flush, which
the orchestration needs for correlation logging.

No binary payloads live here: media is a reference only.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Subject(Base):
    """A student identity, in TutorTwin's own terms."""

    __tablename__ = "tutortwin_subjects"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    external_identity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_identity_value: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "external_identity_type", "external_identity_value", name="uq_subject_external"
        ),
    )


class Entitlement(Base):
    __tablename__ = "entitlements"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    plan_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="local_fake")
    source_version: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (Index("ix_entitlements_subject", "subject_id", "status"),)


class PlanPolicy(Base):
    """Local plan matrix. Phase 09 augments this with the website source of truth."""

    __tablename__ = "plan_policies"

    plan_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    allows_paid_ai: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    features_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    limits_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Tutor(Base):
    __tablename__ = "tutors"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TutorPersonaVersion(Base):
    """Immutable once activated. Persona changes create a new version row."""

    __tablename__ = "tutor_persona_versions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tutor_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutors.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    persona_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tutor_id", "version", name="uq_persona_tutor_version"),
        Index("ix_persona_active", "tutor_id", "is_active"),
    )


class TutorAssignment(Base):
    __tablename__ = "tutor_assignments"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    tutor_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutors.id", ondelete="CASCADE"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_assignment_subject_active", "subject_id", "is_active"),)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    tutor_id: Mapped[UUID | None] = mapped_column(ForeignKey("tutors.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OPEN")
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Load-open-conversation-for-subject is the hot path.
    __table_args__ = (Index("ix_conversation_subject_status", "subject_id", "status"),)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    input_type: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    media_ref_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    capability: Mapped[str | None] = mapped_column(String(64))
    safety_flags_json: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("role in ('STUDENT', 'ASSISTANT', 'SYSTEM')", name="ck_message_role"),
        Index("ix_message_conversation_created", "conversation_id", "created_at"),
    )


class RequestEvent(Base):
    """The inbound normalized event, as received."""

    __tablename__ = "request_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_request_event_correlation", "correlation_id"),)


class RequestState(Base):
    """Terminal state per request. One row per processed request."""

    __tablename__ = "request_states"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    request_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("request_events.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_request_state_event", "request_event_id"),)


class IdempotencyKey(Base):
    """Dedupe on source + message id.

    The unique constraint is the enforcement; the stored response is what a
    duplicate replays. Never re-executes completed work.

    `completed_at` separates "someone is working on this" from "this is done".
    Without it, a container that dies between claiming the key and storing the
    response leaves a permanent empty claim, and every redelivery of that message
    returns an empty success - the student is never answered, forever. With it, a
    claim older than the request timeout is treated as abandoned and re-claimable.
    """

    __tablename__ = "idempotency_keys"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(320), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    response_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("key", name="uq_idempotency_key"),)


class OutboundActionRow(Base):
    __tablename__ = "outbound_actions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    request_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("request_events.id", ondelete="SET NULL")
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    delivery_status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_outbound_conversation", "conversation_id", "created_at"),)


class UsageLedger(Base):
    """Cost evidence. Rate is stored at call time - never recompute with today's price."""

    __tablename__ = "usage_ledger"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="SET NULL")
    )
    request_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("request_events.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_alias: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(128))
    capability: Mapped[str | None] = mapped_column(String(64))
    """What the call was for. Recorded at write time - it cannot be recovered later."""
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    estimated_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rate_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_usage_subject_created", "subject_id", "created_at"),
        Index("ix_usage_capability_created", "capability", "created_at"),
    )


class ModelCatalog(Base):
    """Alias -> vendor model ID + price. Keeps model IDs out of business code."""

    __tablename__ = "model_catalog"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    model_alias: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    input_cost_micros_per_1k: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_cost_micros_per_1k: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rate_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("model_alias", "provider", "model_id", name="uq_model_catalog"),
        Index("ix_model_catalog_alias_active", "model_alias", "is_active"),
    )


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(128))
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    detail_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_audit_created", "created_at"),)


class MediaObject(Base):
    """A student's media, tracked through its state machine.

    Binary content lives in the BlobStore; this row holds the reference, the
    state, and the provenance of anything extracted from it.
    """

    __tablename__ = "media_objects"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_media_id: Mapped[str] = mapped_column(String(256), nullable=False)

    state: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(16))
    reject_reason: Mapped[str | None] = mapped_column(String(48))

    brief: Mapped[str | None] = mapped_column(Text)
    """The instruction that unlocked processing. NULL means still gated."""

    blob_key: Mapped[str | None] = mapped_column(String(512))
    sha256: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # One row per inbound media reference: a redelivered event finds the
        # existing row instead of starting a second pipeline.
        UniqueConstraint("source", "source_media_id", "subject_id", name="uq_media_source"),
        Index("ix_media_subject_state", "subject_id", "state"),
    )


class MediaExtraction(Base):
    """Cached extraction, keyed by content and parser version.

    Owner-scoped: private content must never be served to another student from
    cache, even when the bytes are identical.
    """

    __tablename__ = "media_extractions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    ocr_version: Mapped[str | None] = mapped_column(String(32))
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(24), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int | None] = mapped_column(Integer)
    """Stored as basis points (0-10000) to avoid float comparison in SQL."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "subject_id",
            "sha256",
            "parser_version",
            "page_number",
            "method",
            name="uq_media_extraction",
        ),
        Index("ix_extraction_lookup", "subject_id", "sha256", "parser_version"),
    )


class Job(Base):
    """Durable async work. Cloud Tasks carries only this row's id."""

    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_type: Mapped[str] = mapped_column(String(48), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    idempotency_key: Mapped[str] = mapped_column(String(320), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    owner_subject_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE")
    )
    media_object_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_objects.id", ondelete="CASCADE")
    )
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # The dedupe guarantee: a redelivered event cannot create a second job.
        UniqueConstraint("idempotency_key", name="uq_job_idempotency"),
        Index("ix_job_state_retry", "state", "next_retry_at"),
    )
