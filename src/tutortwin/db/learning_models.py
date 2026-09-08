"""Learning-engine tables: artifacts, decks, assessments, notes, homework tasks.

Kept separate from `db/models.py` and `db/knowledge_models.py` for the same
reason those are separate from each other - one subsystem per module - while
sharing the same `Base` so Alembic sees a single metadata.

Two shapes are worth reading closely:

**`assessment_questions.answer_key` is a column a student query never selects.**
The key, the worked solution and the rubric live in one JSONB column, and the
delivery path projects the row into `StudentQuestion`, which has no field able to
hold any of them. Withholding is structural rather than a filter someone must
remember to apply.

**`flashcard_reviews` is an append-only log, and the schedule is a column on the
card.** The log is the evidence; the column is the derived state. Keeping both
means a scheduling change can be replayed against real review history instead of
being trusted.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
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


class LearningArtifact(Base):
    """A generated diagram. Bytes live in the BlobStore; this row is metadata.

    `generated_by` records whether a model produced the *specification*. It never
    records a model producing the image, because that path does not exist.
    """

    __tablename__ = "learning_artifacts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_format: Mapped[str] = mapped_column(String(8), nullable=False)
    blob_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generated_by: Mapped[str] = mapped_column(String(16), nullable=False, default="deterministic")
    spec: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """The validated specification it was rendered from, so the same picture can
    be reproduced exactly rather than regenerated approximately."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Identical content for the same student is one artifact. Re-asking for
        # the same graph must not accumulate rows or blobs.
        UniqueConstraint("subject_id", "sha256", name="uq_artifact_subject_sha"),
        CheckConstraint(
            "generated_by IN ('deterministic', 'model_spec')",
            name="ck_artifact_generated_by",
        ),
        Index("ix_artifact_subject_created", "subject_id", "created_at"),
    )


class FlashcardDeck(Base):
    __tablename__ = "flashcard_decks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    topic: Mapped[str | None] = mapped_column(String(120))
    source_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("subject_id", "name", name="uq_deck_subject_name"),)


class Flashcard(Base):
    """A card plus its current SM-2 state.

    The schedule columns are derived from `flashcard_reviews` and cached here so
    that finding due cards is one indexed query rather than a replay.
    """

    __tablename__ = "flashcards"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    deck_id: Mapped[UUID] = mapped_column(
        ForeignKey("flashcard_decks.id", ondelete="CASCADE"), nullable=False
    )
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )

    front: Mapped[str] = mapped_column(Text, nullable=False)
    back: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str | None] = mapped_column(String(120))
    difficulty: Mapped[str] = mapped_column(String(16), nullable=False, default="MEDIUM")
    tags: Mapped[list[str] | None] = mapped_column(JSONB)
    source_citation: Mapped[str | None] = mapped_column(String(512))
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ease_factor: Mapped[float] = mapped_column(Float, nullable=False, default=2.5)
    lapses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    due_on: Mapped[date | None] = mapped_column(Date)
    """NULL means never reviewed, which `is_due()` treats as due now."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Regenerating a deck from the same document must not duplicate cards.
        UniqueConstraint("deck_id", "content_sha256", name="uq_card_deck_content"),
        CheckConstraint("ease_factor >= 1.3", name="ck_card_ease_floor"),
        Index("ix_card_due", "subject_id", "due_on"),
    )


class FlashcardReview(Base):
    """Append-only review history. Never updated, never deleted."""

    __tablename__ = "flashcard_reviews"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    card_id: Mapped[UUID] = mapped_column(
        ForeignKey("flashcards.id", ondelete="CASCADE"), nullable=False
    )
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    grade: Mapped[str] = mapped_column(String(8), nullable=False)
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False)
    ease_factor: Mapped[float] = mapped_column(Float, nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_review_card_time", "card_id", "reviewed_at"),)


class Assessment(Base):
    """A quiz or a mock test. One table: they differ by `kind`, not by shape."""

    __tablename__ = "assessments"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    topic: Mapped[str | None] = mapped_column(String(120))
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_marks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blueprint: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    truncated_reason: Mapped[str | None] = mapped_column(String(200))
    """Set when the plan's ceiling produced a smaller paper than requested. The
    student is told; a silently shortened paper would look like a defect."""

    source_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL")
    )
    artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("learning_artifacts.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("kind IN ('QUIZ', 'MOCK_TEST', 'PRACTICE_SET')", name="ck_assessment_kind"),
        Index("ix_assessment_subject_created", "subject_id", "created_at"),
    )


class AssessmentQuestion(Base):
    """One question. The key lives in `answer_key` and is never delivered."""

    __tablename__ = "assessment_questions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    assessment_id: Mapped[UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    question_type: Mapped[str] = mapped_column(String(24), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    marks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    options: Mapped[list[str] | None] = mapped_column(JSONB)

    answer_key: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """`correct_option`, `expected_answer`, `tolerance`, `unit`,
    `worked_solution`, `rubric`. Selected only by the grading path."""

    __table_args__ = (
        UniqueConstraint("assessment_id", "number", name="uq_question_number"),
        CheckConstraint("marks >= 0", name="ck_question_marks"),
    )


class AssessmentAttempt(Base):
    __tablename__ = "assessment_attempts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    assessment_id: Mapped[UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False
    )
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ASSIGNED")
    awarded_marks: Mapped[float | None] = mapped_column(Float)
    total_marks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Calls this attempt actually cost. Asserted in tests, so a regression that
    graded ten MCQs with ten calls fails rather than merely costing money."""

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN ('ASSIGNED', 'IN_PROGRESS', 'SUBMITTED', 'GRADED')",
            name="ck_attempt_state",
        ),
        Index("ix_attempt_subject_state", "subject_id", "state"),
    )


class AttemptResponse(Base):
    """One answer and its grade, with the evidence that produced the grade."""

    __tablename__ = "attempt_responses"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("assessment_attempts.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("assessment_questions.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)

    selected_option: Mapped[int | None] = mapped_column(Integer)
    text_answer: Mapped[str | None] = mapped_column(Text)

    awarded_marks: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_marks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    correct: Mapped[bool | None] = mapped_column(Boolean)
    """NULL for a subjective question: partial credit is not a boolean."""

    feedback: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(String(256))
    graded_by: Mapped[str] = mapped_column(String(16), nullable=False, default="deterministic")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="uq_response_attempt_question"),
        CheckConstraint(
            "graded_by IN ('deterministic', 'model')",
            name="ck_response_graded_by",
        ),
    )


class StudyNoteRow(Base):
    __tablename__ = "study_notes"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    sections: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    sources: Mapped[list[str] | None] = mapped_column(JSONB)
    dropped_citations: Mapped[list[str] | None] = mapped_column(JSONB)
    """Citations the model produced that matched no supplied source. Stored so a
    rise in hallucinated references is visible rather than merely discarded."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_notes_subject_topic", "subject_id", "topic"),)


class HomeworkTaskRow(Base):
    """Per-task state, so "I don't understand step 3" resolves to a real step."""

    __tablename__ = "homework_tasks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    problem_text: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str | None] = mapped_column(String(120))
    stage: Mapped[str] = mapped_column(String(24), nullable=False, default="PRESENTED")
    shown_steps: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB)
    hints_given: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    student_answer: Mapped[str | None] = mapped_column(Text)
    verification_verdict: Mapped[str | None] = mapped_column(String(20))
    verification_method: Mapped[str | None] = mapped_column(String(24))
    media_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_objects.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_homework_conversation", "conversation_id", "updated_at"),)


__all__ = [
    "Assessment",
    "AssessmentAttempt",
    "AssessmentQuestion",
    "AttemptResponse",
    "Flashcard",
    "FlashcardDeck",
    "FlashcardReview",
    "HomeworkTaskRow",
    "LearningArtifact",
    "StudyNoteRow",
]
