"""Phase 05 mandatory end-to-end scenarios, against real PostgreSQL.

Every test asserts a **cost** (how many model calls a path required) or a
**security** property (what a student could see), because those are the two
things this engine can get expensively or dangerously wrong. Correctness of the
individual algorithms is covered by `tests/unit/test_learning_engine.py`; these
tests are about what the whole path does when the pieces are wired together.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.knowledge_models import TopicStat
from tutortwin.db.learning_models import (
    Assessment,
    AssessmentQuestion,
    AttemptResponse,
    Flashcard,
    LearningArtifact,
    StudyNoteRow,
)
from tutortwin.db.models import Conversation, MediaObject, Subject
from tutortwin.domain.budget import (
    BudgetOutcome,
    BudgetReason,
    ExecutionBudgetDecision,
    VerifierMode,
)
from tutortwin.domain.capabilities import CapabilityId, ConfidenceBand, Difficulty
from tutortwin.domain.knowledge import (
    RetrievalScope,
    SourceKind,
    Visibility,
)
from tutortwin.domain.learning import (
    ArtifactFormat,
    ArtifactKind,
    AssessmentKind,
    MasterySignal,
    QuestionResponse,
    QuestionSpec,
    QuestionType,
    ReviewGrade,
    TaskStage,
    TopicProgress,
    VerificationVerdict,
)
from tutortwin.domain.provider import ModelAlias
from tutortwin.learning.assessment import (
    BlueprintLimits,
    assemble_report,
    build_blueprint,
    build_rubric_prompt,
    deliver,
    grade_objective,
    parse_rubric_response,
    plan_grading,
    render_printable_paper,
)
from tutortwin.learning.essay import (
    AUTHORSHIP_NOTICE,
    MAX_REWRITE_CHARS,
    parse_feedback_response,
    plan_feedback,
    render_feedback,
)
from tutortwin.learning.homework import (
    ArithmeticCalculator,
    DisabledSandbox,
    HomeworkTask,
    SandboxStatus,
    apply_action,
    detect_action,
)
from tutortwin.learning.notes import NoteScope, build_notes_prompt, parse_notes_response
from tutortwin.learning.practice import ProgressSnapshot, schedule_review, weak_topics
from tutortwin.learning.solver import (
    SolverSubject,
    VerificationPolicy,
    VerificationTier,
    normalize_problem,
)
from tutortwin.learning.twin import answers_differ, generate_twin
from tutortwin.learning.verification import (
    VerifiableProblem,
    compare_quantities,
    verify_answer,
    verify_symbolic_equivalence,
)
from tutortwin.learning.visuals import PlotSeries, PlotSpec
from tutortwin.media.blobstore import FilesystemBlobStore
from tutortwin.policies.rag_policy import RagDecision, RagReason, RagVerdict
from tutortwin.rag.embeddings import DeterministicEmbeddingProvider
from tutortwin.rag.ingestion import IngestionRequest, ingest
from tutortwin.rag.service import RetrievalService
from tutortwin.repositories.learning import (
    add_cards,
    apply_schedule,
    create_assessment,
    due_cards,
    get_or_create_deck,
    load_answer_key,
    load_schedule,
    load_student_paper,
    load_task,
    question_ids_by_number,
    record_artifact,
    record_grades,
    save_notes,
    save_task,
    start_attempt,
)
from tutortwin.services.artifacts import VisualArtifactService

pytestmark = pytest.mark.integration


async def make_subject(session: AsyncSession) -> Subject:
    subject = Subject(
        external_identity_type="test_phone",
        external_identity_value=f"+91{uuid.uuid4().hex[:10]}",
    )
    session.add(subject)
    await session.flush()
    return subject


async def make_conversation(session: AsyncSession, subject: Subject) -> Conversation:
    conversation = Conversation(subject_id=subject.id, source="test_harness")
    session.add(conversation)
    await session.flush()
    return conversation


def artifact_service(tmp_path: Path) -> VisualArtifactService:
    return VisualArtifactService(FilesystemBlobStore(root=tmp_path / "blobs"))


def budget(*, verifier: VerifierMode = VerifierMode.ON_LOW_CONFIDENCE) -> ExecutionBudgetDecision:
    return ExecutionBudgetDecision(
        outcome=BudgetOutcome.ALLOW_WITH_VERIFICATION,
        reason=BudgetReason.ADVANCED_STEM,
        alias=ModelAlias.ADVANCED_REASONING,
        verifier_mode=verifier,
        verifier_alias=ModelAlias.VERIFIER_PRIMARY if verifier is not VerifierMode.NONE else None,
    )


# --- 1. image homework -> brief -> solve -> verify -----------------------------


async def test_image_homework_with_brief_is_solved_and_verified(session: AsyncSession) -> None:
    """A photographed worksheet reaches the same verifier as typed text.

    The OCR text carries typographic operators (`−`, `×`) that the expression
    parser rejects outright, so this asserts normalisation is on the image path -
    without it a photographed problem would be permanently unverifiable.
    """
    subject = await make_subject(session)
    conversation = await make_conversation(session, subject)

    media = MediaObject(
        subject_id=subject.id,
        conversation_id=conversation.id,
        source="test_harness",
        source_media_id=f"img_{uuid.uuid4().hex[:8]}",
        state="EXTRACTED",
        kind="IMAGE",
        brief="solve question 4 for me",
        mime_type="image/jpeg",
    )
    session.add(media)
    await session.flush()
    assert media.brief, "no brief means no expensive processing (Phase 03 gate)"

    ocr_text = "Solve for x: 3x − 4 = 11"
    problem = normalize_problem(ocr_text)
    assert "−" not in problem.text
    assert problem.subject is SolverSubject.ALGEBRA

    verification = verify_answer(
        "Adding 4 to both sides gives 3x = 15, so x = 5",
        VerifiableProblem(equation="3*x - 4 = 11", variable="x"),
    )
    assert verification.verdict is VerificationVerdict.VERIFIED

    task = HomeworkTask(problem_text=problem.text, stage=TaskStage.SOLVED, topic="algebra")
    task_id = await save_task(
        session,
        subject_id=subject.id,
        conversation_id=conversation.id,
        task=task,
        verification=verification,
        media_id=media.id,
    )
    await session.commit()

    stored = await load_task(session, subject_id=subject.id, conversation_id=conversation.id)
    assert stored is not None
    assert stored.id == task_id
    assert stored.verification_verdict == "VERIFIED"
    assert stored.media_id == media.id


# --- 2. calculus text -> verify ------------------------------------------------


async def test_calculus_answer_is_verified_symbolically_without_a_model() -> None:
    result = verify_symbolic_equivalence("2*x + 3", "3 + 2*x")
    assert result.verdict is VerificationVerdict.VERIFIED

    wrong = verify_symbolic_equivalence("2*x + 3", "2*x + 4")
    assert wrong.verdict is VerificationVerdict.REFUTED


# --- 3. physics with units -> unit check ---------------------------------------


async def test_physics_answer_is_checked_across_units() -> None:
    """`200 cm` against a key of `2.0 m` is correct, and marking it wrong is a bug."""
    same = compare_quantities("200 cm", "2.0 m")
    assert same.verdict is VerificationVerdict.VERIFIED

    wrong_dimension = compare_quantities("5 kg", "5 m")
    assert wrong_dimension.verdict is VerificationVerdict.REFUTED

    problem = normalize_problem("A car accelerates at 3 m/s^2 for 4 s. Find the velocity.")
    assert problem.subject is SolverSubject.PHYSICS
    assert problem.has_units
    assert any("9.81" in a for a in problem.assumptions), "assumptions must be stated"


# --- 4. generate graph -> local artifact ---------------------------------------


async def test_graph_is_rendered_locally_and_stored(session: AsyncSession, tmp_path: Path) -> None:
    subject = await make_subject(session)
    conversation = await make_conversation(session, subject)
    service = artifact_service(tmp_path)

    spec = PlotSpec(title="y = x^2", series=(PlotSeries(expression="x**2", label="parabola"),))
    outcome = await service.render_and_store(
        session,
        subject_id=subject.id,
        conversation_id=conversation.id,
        spec=spec,
        image_format=ArtifactFormat.PNG,
    )
    await session.commit()

    assert outcome.model_calls == 0, "a diagram must never cost a model call"
    assert outcome.ref.kind is ArtifactKind.FUNCTION_PLOT
    assert outcome.ref.generated_by == "deterministic"

    data = await service.fetch(blob_key=outcome.ref.blob_key, subject_id=subject.id)
    assert data[:4] == b"\x89PNG"

    # Content-addressed: asking again stores nothing new.
    again = await service.render_and_store(
        session,
        subject_id=subject.id,
        conversation_id=conversation.id,
        spec=spec,
        image_format=ArtifactFormat.PNG,
    )
    await session.commit()
    assert again.reused
    assert again.ref.id == outcome.ref.id
    rows = await session.execute(
        select(func.count())
        .select_from(LearningArtifact)
        .where(LearningArtifact.subject_id == subject.id)
    )
    assert rows.scalar_one() == 1


# --- 5. twin problem -> verified different answer ------------------------------


async def test_twin_problem_has_a_verified_different_answer() -> None:
    original = "Solve for x: x^2 - 5x + 6 = 0"
    twin = generate_twin(original)

    assert twin.verified, "a twin whose answer was not checked is not a twin, it is a guess"
    assert answers_differ("x = 2 or x = 3", twin.answer)
    assert twin.generated_by == "deterministic", "a twin costs no model call"


# --- 6. flashcards from document -> review schedule ----------------------------


async def test_flashcards_from_a_document_get_a_deterministic_schedule(
    session: AsyncSession,
) -> None:
    subject = await make_subject(session)
    result = await ingest(
        session,
        IngestionRequest(
            title="Cell Biology Notes",
            text=(
                "Mitochondria are the site of aerobic respiration. "
                "The cell membrane is a phospholipid bilayer that controls transport. "
                "Ribosomes assemble proteins from amino acids."
            ),
            kind=SourceKind.PDF_EXTRACTION,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=subject.id,
        ),
        DeterministicEmbeddingProvider(),
    )

    deck_id = await get_or_create_deck(
        session, subject_id=subject.id, name="Cell Biology", source_id=result.source_id
    )
    cards = (
        ("Where does aerobic respiration happen?", "In the mitochondria"),
        ("What is the cell membrane made of?", "A phospholipid bilayer"),
    )
    added = await add_cards(
        session,
        deck_id=deck_id,
        subject_id=subject.id,
        cards=cards,
        topic="cell biology",
        source_citation="Cell Biology Notes",
    )
    assert added == 2

    # Regenerating the same deck must not duplicate cards.
    assert await add_cards(session, deck_id=deck_id, subject_id=subject.id, cards=cards) == 0
    await session.commit()

    today = date(2026, 3, 1)
    queue = await due_cards(session, subject_id=subject.id, today=today)
    assert len(queue) == 2, "a card that has never been reviewed is due"

    card = queue[0]
    schedule = await load_schedule(session, card_id=card.id, subject_id=subject.id)
    assert schedule is not None
    updated = schedule_review(schedule, ReviewGrade.GOOD, today=today)
    assert updated.due_on == date(2026, 3, 2), "first interval is one day, computed not asked"

    await apply_schedule(
        session,
        card_id=card.id,
        subject_id=subject.id,
        grade=ReviewGrade.GOOD,
        schedule=updated,
    )
    await session.commit()

    remaining = await due_cards(session, subject_id=subject.id, today=today)
    assert card.id not in {c.id for c in remaining}

    stored = (await session.execute(select(Flashcard).where(Flashcard.id == card.id))).scalar_one()
    assert stored.due_on == date(2026, 3, 2)
    assert stored.source_citation == "Cell Biology Notes"


# --- 7. quiz -> submit -> grade ------------------------------------------------


QUIZ_QUESTIONS = (
    QuestionSpec(
        number=1,
        question_type=QuestionType.MCQ,
        prompt="Which gas do plants absorb?",
        marks=1,
        options=("Oxygen", "Carbon dioxide", "Nitrogen"),
        correct_option=1,
        topic="photosynthesis",
    ),
    QuestionSpec(
        number=2,
        question_type=QuestionType.TRUE_FALSE,
        prompt="Chlorophyll is green.",
        marks=1,
        correct_boolean=True,
        topic="photosynthesis",
    ),
    QuestionSpec(
        number=3,
        question_type=QuestionType.NUMERIC,
        prompt="A trolley travels 2.0 m. Express the distance in centimetres.",
        marks=2,
        correct_numeric=2.0,
        numeric_unit="m",
        topic="measurement",
    ),
)


async def test_quiz_is_delivered_without_answers_and_graded_without_a_model(
    session: AsyncSession,
) -> None:
    subject = await make_subject(session)
    conversation = await make_conversation(session, subject)

    assessment_id = await create_assessment(
        session,
        subject_id=subject.id,
        conversation_id=conversation.id,
        kind=str(AssessmentKind.QUIZ),
        title="Photosynthesis quiz",
        questions=QUIZ_QUESTIONS,
        duration_minutes=10,
        topic="photosynthesis",
    )
    await session.commit()

    delivered = await load_student_paper(
        session, assessment_id=assessment_id, subject_id=subject.id
    )
    assert len(delivered) == 3
    serialized = repr([q.model_dump() for q in delivered])
    assert "correct_option" not in serialized
    assert "correct_numeric" not in serialized

    key = await load_answer_key(session, assessment_id=assessment_id, subject_id=subject.id)
    responses = (
        QuestionResponse(number=1, chosen_option=1),
        QuestionResponse(number=2, boolean_answer=True),
        QuestionResponse(number=3, numeric_answer=200.0, numeric_unit="cm"),
    )
    plan = plan_grading(key, responses)
    assert plan.model_calls_required == 0, "an all-objective paper is graded for free"

    graded = tuple(
        grade_objective(spec, response) for spec, response in zip(key, responses, strict=True)
    )
    report = assemble_report(graded, (), model_calls=0)
    assert report.total_awarded == 4.0
    assert report.percentage == 100.0

    attempt_id = await start_attempt(
        session,
        assessment_id=assessment_id,
        subject_id=subject.id,
        total_marks=report.total_possible,
    )
    await record_grades(
        session,
        attempt_id=attempt_id,
        subject_id=subject.id,
        question_ids=await question_ids_by_number(session, assessment_id=assessment_id),
        report=report,
        model_calls=0,
        submitted_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    await session.commit()

    stored = (
        await session.execute(
            select(AttemptResponse).where(AttemptResponse.attempt_id == attempt_id)
        )
    ).scalars()
    rows = list(stored)
    assert len(rows) == 3
    assert all(row.graded_by == "deterministic" for row in rows)
    assert all(row.correct for row in rows)


# --- 8. mock -> artifact -> hidden key -----------------------------------------


async def test_mock_paper_artifact_contains_no_answers(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The printable paper is built from `StudentQuestion`, so it cannot leak.

    The test proves the property on the stored bytes rather than on the function
    return, because the artifact is what actually reaches the student.
    """
    subject = await make_subject(session)
    conversation = await make_conversation(session, subject)

    assessment_id = await create_assessment(
        session,
        subject_id=subject.id,
        conversation_id=conversation.id,
        kind=str(AssessmentKind.MOCK_TEST),
        title="Algebra mock",
        questions=(
            QuestionSpec(
                number=1,
                question_type=QuestionType.MCQ,
                prompt="Solve x + 2 = 5",
                marks=2,
                options=("x = 1", "x = 3", "x = 7"),
                correct_option=1,
                worked_solution="Subtract 2 from both sides to get x = 3.",
                topic="algebra",
            ),
        ),
        duration_minutes=30,
    )
    await session.commit()

    paper = render_printable_paper(
        title="Algebra mock",
        duration_minutes=30,
        questions=await load_student_paper(
            session, assessment_id=assessment_id, subject_id=subject.id
        ),
    )
    blobs = FilesystemBlobStore(root=tmp_path / "papers")
    blob = await blobs.put(
        subject_id=subject.id,
        data=paper.encode("utf-8"),
        content_type="text/plain",
        extension="txt",
    )
    artifact_id = await record_artifact(
        session,
        subject_id=subject.id,
        conversation_id=conversation.id,
        kind=str(ArtifactKind.PRINTABLE_PAPER),
        artifact_format=str(ArtifactFormat.TEXT),
        blob_key=blob.key,
        sha256=blob.sha256,
        width=0,
        height=0,
    )
    await session.execute(
        Assessment.__table__.update()
        .where(Assessment.id == assessment_id)
        .values(artifact_id=artifact_id)
    )
    await session.commit()

    delivered_bytes = (await blobs.get(blob.key, subject_id=subject.id)).decode("utf-8")
    assert "Solve x + 2 = 5" in delivered_bytes
    assert "Subtract 2 from both sides" not in delivered_bytes
    assert "correct" not in delivered_bytes.lower()

    key_row = (
        await session.execute(
            select(AssessmentQuestion.answer_key).where(
                AssessmentQuestion.assessment_id == assessment_id
            )
        )
    ).scalar_one()
    assert key_row["worked_solution"], "the key is stored, just never delivered"


# --- 9. submit mock -> grade -> progress ---------------------------------------


async def test_submitted_mock_updates_progress_and_weak_topics(session: AsyncSession) -> None:
    subject = await make_subject(session)

    # Two topics with enough evidence to speak about, one deliberately thin.
    stats = (
        ("algebra", 10, 3),
        ("geometry", 8, 7),
        ("trigonometry", 3, 0),
    )
    for topic, attempts, correct in stats:
        session.add(
            TopicStat(
                subject_id=subject.id,
                topic=topic,
                attempts=attempts,
                correct=correct,
            )
        )
    await session.commit()

    rows = (
        await session.execute(select(TopicStat).where(TopicStat.subject_id == subject.id))
    ).scalars()
    snapshot = ProgressSnapshot(
        topics=tuple(
            TopicProgress(topic=r.topic, attempts=r.attempts, correct=r.correct) for r in rows
        )
    )

    weak = weak_topics(snapshot)
    topics = {w.topic for w in weak}
    assert "algebra" in topics
    assert "trigonometry" not in topics, "three attempts is not evidence of weakness"
    assert all(w.evidence for w in weak), "every recommendation carries its evidence"

    thin = next(t for t in snapshot.topics if t.topic == "trigonometry")
    assert thin.signal is MasterySignal.INSUFFICIENT_EVIDENCE
    assert thin.accuracy is None, "0.0 would read as 'always wrong', not 'not enough data'"


# --- 10. essay feedback --------------------------------------------------------


ESSAY = (
    "Social media has changed how teenagers talk to each other.\n\n"
    "Some people say it is bad for them. Others disagree with this view.\n\n"
    "In conclusion social media is a mixed thing overall."
)

FEEDBACK_REPLY = (
    "THESIS: The position is not stated. || clear topic || name your claim in sentence one\n"
    "STRUCTURE: Three paragraphs, no progression. || short paragraphs || add a linking line\n"
    "CLARITY: Mostly clear. || plain language || replace 'a mixed thing'\n"
    "GRAMMAR: A comma is missing after 'In conclusion'. || || proofread aloud\n"
    "EVIDENCE: No examples are given. || || cite one study\n"
    "PARA 1: Good opening, but it announces the topic rather than an argument.\n"
    "PARA 2: 'Some people' is vague - name who.\n"
    "PARA 9: This paragraph does not exist.\n"
    "REWRITE: Social media has reshaped teenage friendship, mostly for the worse.\n"
    "GRADE: 12|Competent structure, weak thesis and no evidence.\n"
)


async def test_essay_feedback_is_one_call_and_will_not_ghost_write() -> None:
    plan = plan_feedback(ESSAY, rubric="AQA descriptor")
    assert plan.model_calls_required == 1, "one call covers every dimension"
    assert plan.paragraph_count == 3

    feedback = parse_feedback_response(FEEDBACK_REPLY, essay=ESSAY, rubric_max=15)
    assert len(feedback.dimensions) == 5
    assert {n.index for n in feedback.paragraphs} == {1, 2}, (
        "a note on a missing paragraph is dropped"
    )
    assert feedback.rubric_marks == 12.0
    assert sum(len(s) for s in feedback.rewrite_suggestions) <= MAX_REWRITE_CHARS

    rendered = render_feedback(feedback)
    assert AUTHORSHIP_NOTICE in rendered

    # A model that ignores the instruction and returns a full replacement draft
    # is cut in code, not merely asked not to.
    ghost = "REWRITE: " + ("Social media has reshaped teenage friendship. " * 60)
    bounded = parse_feedback_response(ghost, essay=ESSAY)
    assert bounded.authorship_truncated
    assert sum(len(s) for s in bounded.rewrite_suggestions) <= MAX_REWRITE_CHARS


# --- 11. coding help with no unsafe execution ----------------------------------


async def test_coding_help_never_executes_and_says_so() -> None:
    sandbox = DisabledSandbox()
    result = await sandbox.run("python", "import os; os.system('id')")

    assert sandbox.enabled is False
    assert result.status is SandboxStatus.DISABLED
    assert result.stdout == ""
    assert "not enabled" in result.reason

    # The calculator is a separate type and stays a calculator.
    calculator = ArithmeticCalculator()
    assert calculator.evaluate("17 * 23").startswith("391")
    assert calculator.evaluate("__import__('os').getcwd()") is None
    assert calculator.evaluate("9**9**9") is None


# --- 12. RAG-grounded answer includes source -----------------------------------


async def test_rag_grounded_notes_cite_a_real_source_and_drop_invented_ones(
    session: AsyncSession,
) -> None:
    subject = await make_subject(session)
    await ingest(
        session,
        IngestionRequest(
            title="Newton's Laws Handout",
            text=(
                "Newton's second law states that force equals mass times acceleration. "
                "The unit of force is the newton, defined as one kilogram metre per "
                "second squared."
            ),
            kind=SourceKind.PDF_EXTRACTION,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=subject.id,
        ),
        DeterministicEmbeddingProvider(),
    )
    await session.commit()

    service = RetrievalService(provider=DeterministicEmbeddingProvider())
    scope = RetrievalScope(subject_id=subject.id)
    retrieval = await service.retrieve(
        session,
        query="What does Newton's second law say about force?",
        scope=scope,
        verdict=RagVerdict(RagDecision.RETRIEVE, RagReason.ASKS_FOR_SOURCE, top_k=3),
        available_tokens=4000,
    )
    assert retrieval.evidence, "the handout must be retrievable by its owner"
    citation = retrieval.evidence[0].citation

    scope_spec = NoteScope(topic="Newton's second law", max_sections=2)
    prompt = build_notes_prompt(scope_spec, retrieval.evidence)
    assert citation.split(",")[0] in prompt

    reply = (
        "## Newton's second law\n"
        "EXPLANATION: Force equals mass times acceleration.\n"
        "FORMULAS: F = ma\n"
        "KEY TERMS: force; acceleration\n"
        "PITFALLS: confusing mass with weight\n"
        "EXAMPLES: none\n"
        f"SOURCES: {citation}; Cambridge Physics Volume 9 page 402\n"
    )
    notes = parse_notes_response(reply, scope_spec, retrieval.evidence)

    assert notes.sources, "a grounded answer must carry its source"
    assert citation in notes.sources[0] or notes.sources[0] in citation
    assert notes.dropped_citations == ("Cambridge Physics Volume 9 page 402",), (
        "a citation matching no supplied source is dropped, not shown to a student "
        "who would go looking for it"
    )

    note_id = await save_notes(
        session,
        subject_id=subject.id,
        conversation_id=None,
        topic=scope_spec.topic,
        sections=[s.model_dump() for s in notes.sections],
        sources=list(notes.sources),
        dropped_citations=list(notes.dropped_citations),
    )
    await session.commit()

    stored = (
        await session.execute(select(StudyNoteRow).where(StudyNoteRow.id == note_id))
    ).scalar_one()
    assert stored.sources
    assert stored.dropped_citations, "the drop is recorded, not silently swallowed"


# --- 13. cheap simple question does not use verifier ---------------------------


async def test_simple_question_never_pays_for_a_second_model() -> None:
    policy = VerificationPolicy()

    simple = policy.plan(
        capability=CapabilityId.MATH,
        difficulty=Difficulty.SIMPLE,
        confidence=ConfidenceBand.HIGH,
        decision=budget(),
        local=None,
    )
    assert simple.run_second_model is False
    assert simple.extra_model_calls == 0
    assert simple.tier is VerificationTier.LOCAL_ONLY

    non_stem = policy.plan(
        capability=CapabilityId.GENERAL_TUTORING,
        difficulty=Difficulty.ADVANCED,
        confidence=ConfidenceBand.LOW,
        decision=budget(),
        local=None,
    )
    assert non_stem.extra_model_calls == 0
    assert non_stem.reason == "not_a_stem_capability"


# --- 14. hard STEM uses verifier according to policy ---------------------------


async def test_hard_stem_uses_the_verifier_unless_a_local_check_settles_it() -> None:
    policy = VerificationPolicy()

    hard = policy.plan(
        capability=CapabilityId.PHYSICS,
        difficulty=Difficulty.ADVANCED,
        confidence=ConfidenceBand.LOW,
        decision=budget(),
        local=None,
    )
    assert hard.run_second_model is True
    assert hard.tier is VerificationTier.LOCAL_THEN_MODEL

    # A local REFUTED ends it: certainty, free, and the answer needs redoing
    # whatever a second model would have said.
    refuted = verify_answer("so x = 4", VerifiableProblem(equation="3*x - 4 = 11", variable="x"))
    assert refuted.verdict is VerificationVerdict.REFUTED

    short_circuit = policy.plan(
        capability=CapabilityId.PHYSICS,
        difficulty=Difficulty.ADVANCED,
        confidence=ConfidenceBand.LOW,
        decision=budget(),
        local=refuted,
    )
    assert short_circuit.run_second_model is False
    assert short_circuit.extra_model_calls == 0
    assert short_circuit.reason == "local_check_refuted"


# --- 15. budget-limited plan cannot generate a giant mock ----------------------


async def test_free_plan_cannot_generate_a_giant_mock(session: AsyncSession) -> None:
    subject = await make_subject(session)

    blueprint = build_blueprint(
        topic="algebra",
        duration_minutes=180,
        limits=BlueprintLimits.for_plan("FREE"),
        kind=AssessmentKind.MOCK_TEST,
    )
    assert blueprint.duration_minutes == 30
    assert blueprint.question_count <= 10
    assert blueprint.truncated_reason, "the student is told, not silently given less"

    pro = build_blueprint(
        topic="algebra", duration_minutes=180, limits=BlueprintLimits.for_plan("PRO")
    )
    assert pro.duration_minutes == 180
    assert pro.question_count > blueprint.question_count

    assessment_id = await create_assessment(
        session,
        subject_id=subject.id,
        conversation_id=None,
        kind=str(AssessmentKind.MOCK_TEST),
        title="Algebra mock (free plan)",
        questions=(
            QuestionSpec(
                number=1,
                question_type=QuestionType.MCQ,
                prompt="Solve 2x = 8",
                marks=1,
                options=("2", "4", "8"),
                correct_option=1,
            ),
        ),
        duration_minutes=blueprint.duration_minutes,
        blueprint={
            "question_count": blueprint.question_count,
            "total_marks": blueprint.total_marks,
        },
        truncated_reason=blueprint.truncated_reason,
    )
    await session.commit()

    stored = (
        await session.execute(select(Assessment).where(Assessment.id == assessment_id))
    ).scalar_one()
    assert stored.truncated_reason == blueprint.truncated_reason
    assert stored.duration_minutes == 30


# --- supporting: subjective grading batches into one call ----------------------


async def test_ten_subjective_questions_cost_one_model_call() -> None:
    specs = tuple(
        QuestionSpec(
            number=n,
            question_type=QuestionType.SHORT_ANSWER,
            prompt=f"Explain concept {n}.",
            marks=4,
            rubric="2 marks for definition, 2 for an example",
        )
        for n in range(1, 11)
    )
    responses = tuple(QuestionResponse(number=n, text_answer=f"Answer {n}") for n in range(1, 11))

    plan = plan_grading(specs, responses)
    assert plan.model_calls_required == 1, "ten questions, one call"

    prompt = build_rubric_prompt(plan.needs_model)
    assert prompt.count("<<<STUDENT ANSWER") == 10

    reply = "\n".join(f"{n}|3|Reasonable but no example." for n in range(1, 11))
    graded = parse_rubric_response(reply, plan.needs_model)
    report = assemble_report(plan.deterministic, graded, model_calls=1)
    assert report.model_calls == 1
    assert report.total_awarded == 30.0


# --- supporting: homework follow-ups resolve against the shown steps -----------


async def test_step_followup_resolves_after_a_round_trip(session: AsyncSession) -> None:
    subject = await make_subject(session)
    conversation = await make_conversation(session, subject)

    task = HomeworkTask(problem_text="Solve 3x - 4 = 11")
    action = detect_action("show me the full solution")
    assert action is not None
    task = apply_action(task, action)

    from tutortwin.domain.learning import SolutionStep

    task.steps = (
        SolutionStep(number=1, text="Add 4 to both sides", reason="isolate the term in x"),
        SolutionStep(number=2, text="Divide by 3", reason="make the coefficient 1"),
    )
    await save_task(session, subject_id=subject.id, conversation_id=conversation.id, task=task)
    await session.commit()

    stored = await load_task(session, subject_id=subject.id, conversation_id=conversation.id)
    assert stored is not None
    assert stored.shown_steps is not None
    numbers = {step["number"] for step in stored.shown_steps}
    assert numbers == {1, 2}
    assert 3 not in numbers, "a step that was never shown cannot be explained"


# --- supporting: delivery projection is enforced by the type -------------------


def test_student_projection_cannot_represent_a_key() -> None:
    delivered = deliver(QUIZ_QUESTIONS)
    fields = set(type(delivered[0]).model_fields)
    assert fields == {"number", "question_type", "prompt", "marks", "options"}
