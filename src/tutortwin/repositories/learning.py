"""Persistence for the learning engine.

**Delivery never selects the answer key.** `load_student_paper()` reads only the
columns a student may see and returns `StudentQuestion`, a type with no field
capable of holding a key. Grading uses a separate function that does read it. The
split is the enforcement: there is no code path where a delivery response could
accidentally carry `answer_key`, because the object it builds cannot represent
it.

**Ownership is a WHERE clause, never a Python filter.** Every read takes a
`subject_id` and constrains on it in SQL, for the same reason retrieval does:
fetching another student's row and discarding it in the application is one
refactor away from returning it.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.learning_models import (
    Assessment,
    AssessmentAttempt,
    AssessmentQuestion,
    AttemptResponse,
    Flashcard,
    FlashcardDeck,
    FlashcardReview,
    HomeworkTaskRow,
    LearningArtifact,
    StudyNoteRow,
)
from tutortwin.domain.learning import (
    AttemptState,
    CardSchedule,
    GradedQuestion,
    GradeReport,
    QuestionSpec,
    QuestionType,
    ReviewGrade,
    StudentQuestion,
    VerificationResult,
)
from tutortwin.learning.homework import HomeworkTask
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)


def content_key(front: str, back: str) -> str:
    """Card identity is its content, so regenerating a deck cannot duplicate it."""
    digest = hashlib.sha256()
    digest.update(front.strip().lower().encode("utf-8"))
    digest.update(b"\x00")
    digest.update(back.strip().lower().encode("utf-8"))
    return digest.hexdigest()


# --- artifacts ----------------------------------------------------------------


async def record_artifact(
    session: AsyncSession,
    *,
    subject_id: UUID,
    conversation_id: UUID | None,
    kind: str,
    artifact_format: str,
    blob_key: str,
    sha256: str,
    width: int,
    height: int,
    generated_by: str = "deterministic",
    spec: dict[str, Any] | None = None,
) -> UUID:
    """Idempotent on (subject, sha256): the same picture is one row."""
    statement = (
        pg_insert(LearningArtifact)
        .values(
            subject_id=subject_id,
            conversation_id=conversation_id,
            kind=kind,
            artifact_format=artifact_format,
            blob_key=blob_key,
            sha256=sha256,
            width=width,
            height=height,
            generated_by=generated_by,
            spec=spec,
        )
        .on_conflict_do_nothing(index_elements=["subject_id", "sha256"])
        .returning(LearningArtifact.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is not None:
        return inserted
    existing = (
        await session.execute(
            select(LearningArtifact.id).where(
                LearningArtifact.subject_id == subject_id,
                LearningArtifact.sha256 == sha256,
            )
        )
    ).scalar_one()
    return existing


# --- flashcards ---------------------------------------------------------------


async def get_or_create_deck(
    session: AsyncSession,
    *,
    subject_id: UUID,
    name: str,
    topic: str | None = None,
    source_id: UUID | None = None,
) -> UUID:
    existing = (
        await session.execute(
            select(FlashcardDeck.id).where(
                FlashcardDeck.subject_id == subject_id,
                FlashcardDeck.name == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = FlashcardDeck(subject_id=subject_id, name=name, topic=topic, source_id=source_id)
    session.add(row)
    await session.flush()
    return row.id


async def add_cards(
    session: AsyncSession,
    *,
    deck_id: UUID,
    subject_id: UUID,
    cards: tuple[tuple[str, str], ...],
    topic: str | None = None,
    difficulty: str = "MEDIUM",
    tags: list[str] | None = None,
    source_citation: str | None = None,
) -> int:
    """Returns how many cards were actually new."""
    added = 0
    for front, back in cards:
        statement = (
            pg_insert(Flashcard)
            .values(
                deck_id=deck_id,
                subject_id=subject_id,
                front=front,
                back=back,
                topic=topic,
                difficulty=difficulty,
                tags=tags,
                source_citation=source_citation,
                content_sha256=content_key(front, back),
            )
            .on_conflict_do_nothing(index_elements=["deck_id", "content_sha256"])
            .returning(Flashcard.id)
        )
        if (await session.execute(statement)).scalar_one_or_none() is not None:
            added += 1
    return added


async def due_cards(
    session: AsyncSession, *, subject_id: UUID, today: date, limit: int = 20
) -> list[Flashcard]:
    """Due-now cards, most overdue first, bounded.

    `due_on IS NULL` is a new card, which is due. Expressed in SQL so the queue
    is one indexed read rather than a scan of every card the student owns.
    """
    rows = await session.execute(
        select(Flashcard)
        .where(
            Flashcard.subject_id == subject_id,
            (Flashcard.due_on.is_(None)) | (Flashcard.due_on <= today),
        )
        .order_by(Flashcard.due_on.asc().nullsfirst(), Flashcard.lapses.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def apply_schedule(
    session: AsyncSession,
    *,
    card_id: UUID,
    subject_id: UUID,
    grade: ReviewGrade,
    schedule: CardSchedule,
) -> None:
    """Write the derived state and append the evidence, in one transaction.

    The review log is what a future scheduling change can be replayed against;
    the columns are only a cache of the current answer.
    """
    await session.execute(
        update(Flashcard)
        .where(Flashcard.id == card_id, Flashcard.subject_id == subject_id)
        .values(
            repetitions=schedule.repetitions,
            interval_days=schedule.interval_days,
            ease_factor=schedule.ease_factor,
            lapses=schedule.lapses,
            due_on=schedule.due_on,
        )
    )
    session.add(
        FlashcardReview(
            card_id=card_id,
            subject_id=subject_id,
            grade=str(grade),
            interval_days=schedule.interval_days,
            ease_factor=schedule.ease_factor,
            due_on=schedule.due_on or date.today(),
        )
    )


async def load_schedule(
    session: AsyncSession, *, card_id: UUID, subject_id: UUID
) -> CardSchedule | None:
    row = (
        await session.execute(
            select(Flashcard).where(Flashcard.id == card_id, Flashcard.subject_id == subject_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return CardSchedule(
        repetitions=row.repetitions,
        interval_days=row.interval_days,
        ease_factor=row.ease_factor,
        due_on=row.due_on,
        lapses=row.lapses,
    )


# --- assessments --------------------------------------------------------------


async def create_assessment(
    session: AsyncSession,
    *,
    subject_id: UUID,
    conversation_id: UUID | None,
    kind: str,
    title: str,
    questions: tuple[QuestionSpec, ...],
    duration_minutes: int = 0,
    topic: str | None = None,
    blueprint: dict[str, Any] | None = None,
    truncated_reason: str | None = None,
    source_id: UUID | None = None,
    artifact_id: UUID | None = None,
) -> UUID:
    """Persist a paper. The key goes into `answer_key` and stays there."""
    assessment = Assessment(
        subject_id=subject_id,
        conversation_id=conversation_id,
        kind=kind,
        title=title,
        topic=topic,
        duration_minutes=duration_minutes,
        total_marks=sum(q.marks for q in questions),
        blueprint=blueprint,
        truncated_reason=truncated_reason,
        source_id=source_id,
        artifact_id=artifact_id,
    )
    session.add(assessment)
    await session.flush()

    for spec in questions:
        session.add(
            AssessmentQuestion(
                assessment_id=assessment.id,
                number=spec.number,
                question_type=str(spec.question_type),
                prompt=spec.prompt,
                marks=spec.marks,
                options=list(spec.options) or None,
                answer_key={
                    "correct_option": spec.correct_option,
                    "correct_boolean": spec.correct_boolean,
                    "correct_numeric": spec.correct_numeric,
                    "numeric_unit": spec.numeric_unit,
                    "numeric_tolerance": spec.numeric_tolerance,
                    "expected_answer": spec.expected_answer,
                    "worked_solution": spec.worked_solution,
                    "rubric": spec.rubric,
                    "topic": spec.topic,
                },
            )
        )
    await session.flush()
    return assessment.id


async def load_student_paper(
    session: AsyncSession, *, assessment_id: UUID, subject_id: UUID
) -> tuple[StudentQuestion, ...]:
    """Delivery read. `answer_key` is not in the select list at all.

    The projection is into a type with no answer fields, so even a future column
    added to the key cannot leak through this path.
    """
    rows = await session.execute(
        select(
            AssessmentQuestion.number,
            AssessmentQuestion.question_type,
            AssessmentQuestion.prompt,
            AssessmentQuestion.marks,
            AssessmentQuestion.options,
        )
        .join(Assessment, Assessment.id == AssessmentQuestion.assessment_id)
        .where(
            AssessmentQuestion.assessment_id == assessment_id,
            Assessment.subject_id == subject_id,
        )
        .order_by(AssessmentQuestion.number)
    )
    return tuple(
        StudentQuestion(
            number=number,
            question_type=QuestionType(question_type),
            prompt=prompt,
            marks=marks,
            options=tuple(options or ()),
        )
        for number, question_type, prompt, marks, options in rows.all()
    )


async def load_answer_key(
    session: AsyncSession, *, assessment_id: UUID, subject_id: UUID
) -> tuple[QuestionSpec, ...]:
    """Grading read. Separate function, separate call site, ownership enforced."""
    rows = await session.execute(
        select(AssessmentQuestion)
        .join(Assessment, Assessment.id == AssessmentQuestion.assessment_id)
        .where(
            AssessmentQuestion.assessment_id == assessment_id,
            Assessment.subject_id == subject_id,
        )
        .order_by(AssessmentQuestion.number)
    )
    specs: list[QuestionSpec] = []
    for row in rows.scalars():
        key: dict[str, Any] = row.answer_key or {}
        specs.append(
            QuestionSpec(
                number=row.number,
                question_type=QuestionType(row.question_type),
                prompt=row.prompt,
                marks=row.marks,
                options=tuple(row.options or ()),
                correct_option=key.get("correct_option"),
                correct_boolean=key.get("correct_boolean"),
                correct_numeric=key.get("correct_numeric"),
                numeric_unit=key.get("numeric_unit"),
                numeric_tolerance=key.get("numeric_tolerance") or 0.01,
                expected_answer=key.get("expected_answer"),
                worked_solution=key.get("worked_solution") or "",
                rubric=key.get("rubric"),
                topic=key.get("topic") or "",
            )
        )
    return tuple(specs)


async def start_attempt(
    session: AsyncSession, *, assessment_id: UUID, subject_id: UUID, total_marks: int
) -> UUID:
    attempt = AssessmentAttempt(
        assessment_id=assessment_id,
        subject_id=subject_id,
        state=str(AttemptState.ASSIGNED),
        total_marks=total_marks,
    )
    session.add(attempt)
    await session.flush()
    return attempt.id


async def record_grades(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    subject_id: UUID,
    question_ids: dict[int, UUID],
    report: GradeReport,
    model_calls: int,
    submitted_at: datetime | None = None,
) -> None:
    """Persist the report, its evidence and what it cost."""
    for graded in report.graded:
        question_id = question_ids.get(graded.number)
        if question_id is None:
            continue
        session.add(_response_row(attempt_id, question_id, graded))

    await session.execute(
        update(AssessmentAttempt)
        .where(AssessmentAttempt.id == attempt_id, AssessmentAttempt.subject_id == subject_id)
        .values(
            state=str(AttemptState.GRADED),
            awarded_marks=report.total_awarded,
            total_marks=report.total_possible,
            manual_review=report.needs_review,
            model_calls=model_calls,
            submitted_at=submitted_at or datetime.now().astimezone(),
        )
    )


def _response_row(attempt_id: UUID, question_id: UUID, graded: GradedQuestion) -> AttemptResponse:
    return AttemptResponse(
        attempt_id=attempt_id,
        question_id=question_id,
        number=graded.number,
        awarded_marks=graded.awarded,
        max_marks=graded.possible,
        correct=graded.correct,
        feedback=graded.feedback or None,
        evidence=(graded.evidence[:256] or None),
        graded_by=graded.graded_by,
        needs_review=graded.needs_manual_review,
    )


async def question_ids_by_number(session: AsyncSession, *, assessment_id: UUID) -> dict[int, UUID]:
    rows = await session.execute(
        select(AssessmentQuestion.number, AssessmentQuestion.id).where(
            AssessmentQuestion.assessment_id == assessment_id
        )
    )
    return {number: question_id for number, question_id in rows.all()}


# --- notes --------------------------------------------------------------------


async def save_notes(
    session: AsyncSession,
    *,
    subject_id: UUID,
    conversation_id: UUID | None,
    topic: str,
    sections: list[dict[str, Any]],
    sources: list[str],
    dropped_citations: list[str],
) -> UUID:
    row = StudyNoteRow(
        subject_id=subject_id,
        conversation_id=conversation_id,
        topic=topic,
        sections=sections,
        sources=sources or None,
        dropped_citations=dropped_citations or None,
    )
    session.add(row)
    await session.flush()
    return row.id


# --- homework tasks -----------------------------------------------------------


async def save_task(
    session: AsyncSession,
    *,
    subject_id: UUID,
    conversation_id: UUID,
    task: HomeworkTask,
    verification: VerificationResult | None = None,
    media_id: UUID | None = None,
) -> UUID:
    """Upsert the live task for a conversation.

    One task per conversation at a time: "step 3" has to mean the step 3 the
    student is looking at, and a second open task makes that ambiguous.
    """
    existing = (
        await session.execute(
            select(HomeworkTaskRow).where(
                HomeworkTaskRow.conversation_id == conversation_id,
                HomeworkTaskRow.subject_id == subject_id,
            )
        )
    ).scalar_one_or_none()

    steps = [{"number": s.number, "text": s.text, "reason": s.reason} for s in task.steps]

    if existing is None:
        row = HomeworkTaskRow(
            subject_id=subject_id,
            conversation_id=conversation_id,
            problem_text=task.problem_text,
            topic=task.topic or None,
            stage=str(task.stage),
            shown_steps=steps,
            hints_given=task.hints_given,
            student_answer=task.student_answer,
            verification_verdict=str(verification.verdict) if verification else None,
            verification_method=str(verification.method) if verification else None,
            media_id=media_id,
        )
        session.add(row)
        await session.flush()
        return row.id

    existing.problem_text = task.problem_text
    existing.topic = task.topic or None
    existing.stage = str(task.stage)
    existing.shown_steps = steps
    existing.hints_given = task.hints_given
    existing.student_answer = task.student_answer
    existing.verification_verdict = str(verification.verdict) if verification else None
    existing.verification_method = str(verification.method) if verification else None
    if media_id is not None:
        existing.media_id = media_id
    await session.flush()
    return existing.id


async def load_task(
    session: AsyncSession, *, subject_id: UUID, conversation_id: UUID
) -> HomeworkTaskRow | None:
    return (
        await session.execute(
            select(HomeworkTaskRow).where(
                HomeworkTaskRow.conversation_id == conversation_id,
                HomeworkTaskRow.subject_id == subject_id,
            )
        )
    ).scalar_one_or_none()


__all__ = [
    "add_cards",
    "apply_schedule",
    "content_key",
    "create_assessment",
    "due_cards",
    "get_or_create_deck",
    "load_answer_key",
    "load_schedule",
    "load_student_paper",
    "load_task",
    "question_ids_by_number",
    "record_artifact",
    "record_grades",
    "save_notes",
    "save_task",
    "start_attempt",
]
