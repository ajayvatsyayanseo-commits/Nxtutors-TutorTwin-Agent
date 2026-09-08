"""Knowledge, retrieval and memory tables.

Kept separate from `db/models.py` because they form their own subsystem, but
they share the same `Base` so Alembic sees one metadata.

The ownership columns on `document_chunks` are deliberately denormalized from
`knowledge_sources`. Retrieval filters on them directly, without a join, because
the visibility predicate must be part of the same WHERE clause that ranks - not
a join the planner might reorder or a filter applied afterwards.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from tutortwin.db.models import Base

# Exactly one owner column must be set for each non-global visibility class.
# Expressed in SQL so an unowned private source cannot be inserted at all.
_VISIBILITY_OWNER_CHECK = (
    "(visibility = 'STUDENT_PRIVATE' AND subject_id IS NOT NULL)"
    " OR (visibility = 'TUTOR' AND tutor_id IS NOT NULL)"
    " OR (visibility = 'COURSE' AND course_id IS NOT NULL)"
    " OR (visibility = 'CONVERSATION' AND conversation_id IS NOT NULL)"
    " OR visibility = 'GLOBAL_CURATED'"
)


class KnowledgeSource(Base):
    """An ingested document. Its visibility governs every chunk it owns."""

    __tablename__ = "knowledge_sources"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    visibility: Mapped[str] = mapped_column(String(24), nullable=False)

    subject_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE")
    )
    tutor_id: Mapped[UUID | None] = mapped_column(ForeignKey("tutors.id", ondelete="CASCADE"))
    course_id: Mapped[str | None] = mapped_column(String(64))
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )

    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(32), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Re-ingesting identical content under identical versions is a no-op
        # rather than a second copy. This is what makes ingestion idempotent
        # and stops a re-upload from paying to embed the same text twice.
        UniqueConstraint(
            "content_sha256",
            "visibility",
            "subject_id",
            "tutor_id",
            "parser_version",
            "chunker_version",
            "embedding_model",
            name="uq_source_content",
        ),
        CheckConstraint(_VISIBILITY_OWNER_CHECK, name="ck_source_visibility_owner"),
        Index("ix_source_subject", "subject_id", "visibility"),
        Index("ix_source_tutor", "tutor_id", "visibility"),
    )


class DocumentChunk(Base):
    """One retrievable passage, carrying its own ownership."""

    __tablename__ = "document_chunks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(512))
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    visibility: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE")
    )
    tutor_id: Mapped[UUID | None] = mapped_column(ForeignKey("tutors.id", ondelete="CASCADE"))
    course_id: Mapped[str | None] = mapped_column(String(64))
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )

    embedding_json: Mapped[list[float] | None] = mapped_column(JSONB)
    """JSONB rather than a `vector` column so the schema works with or without
    the pgvector extension. See `rag/vector_store.py` for the trade-off and the
    upgrade path."""

    embedding_model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("source_id", "ordinal", name="uq_chunk_ordinal"),
        CheckConstraint(_VISIBILITY_OWNER_CHECK, name="ck_chunk_visibility_owner"),
        Index("ix_chunk_visibility_subject", "visibility", "subject_id"),
        Index("ix_chunk_visibility_tutor", "visibility", "tutor_id"),
        Index("ix_chunk_source", "source_id"),
    )


class EmbeddingCache(Base):
    """Content-addressed embedding cache.

    Keyed by normalized text hash + model, so identical text is never embedded
    twice whichever document it appeared in. An embedding vector is derived from
    text but is not the text, and the key is a hash - so unlike the extraction
    cache (which stores content and is owner-scoped) this one is safely global.
    """

    __tablename__ = "embedding_cache"

    normalized_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding_model: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding_json: Mapped[list[float]] = mapped_column(JSONB, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StudentMemoryRow(Base):
    """Durable learning facts. Deliberately not a transcript."""

    __tablename__ = "student_memories"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    statement_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    observed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    derived_by: Mapped[str] = mapped_column(String(16), nullable=False, default="deterministic")

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("student_memories.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # Re-observing the same fact increments a counter rather than inserting
        # a duplicate. This is what keeps memory small enough to be useful.
        UniqueConstraint("subject_id", "statement_sha256", name="uq_memory_statement"),
        Index("ix_memory_subject_active", "subject_id", "kind", "superseded_by"),
    )


class TopicStat(Base):
    """Observed facts only - deterministic counters, never model inference."""

    __tablename__ = "topic_stats"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hints_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("subject_id", "topic", name="uq_topic_stat"),
        Index("ix_topic_subject", "subject_id"),
    )


class ConversationSummaryRow(Base):
    """Rolling summary with a monotonic coverage watermark.

    `covered_message_count` counts rather than pointing at a message id: uuid4
    has no ordering, so an id-based watermark could not tell which of two
    concurrent updates covered more.
    """

    __tablename__ = "conversation_summaries"

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    covered_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persona_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    generated_by: Mapped[str] = mapped_column(String(16), nullable=False, default="deterministic")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RetrievalEventRow(Base):
    """Audit trail: what was retrieved, for whom, and what it cost."""

    __tablename__ = "retrieval_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    request_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("request_events.id", ondelete="SET NULL")
    )
    performed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    skip_reason: Mapped[str | None] = mapped_column(String(48))
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    returned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    query_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_ids_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_retrieval_subject_created", "subject_id", "created_at"),)
