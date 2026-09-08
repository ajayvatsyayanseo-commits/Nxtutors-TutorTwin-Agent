"""Learning-engine domain types.

Two distinctions run through this module and are load-bearing everywhere else:

* **Verified vs asserted.** A `VerificationVerdict` says what was actually
  checked. `NOT_APPLICABLE` is the common, unembarrassing case - most tutoring
  answers are prose and cannot be machine-checked. Claiming more than that is the
  worst failure this subsystem can have.
* **Observed vs inferred.** A count of attempts is a measurement; "weak at
  trigonometry" is an interpretation. They never share a field.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- verification -------------------------------------------------------------


class VerificationVerdict(StrEnum):
    VERIFIED = "VERIFIED"
    """A deterministic check passed. Not "probably right" - actually checked."""

    REFUTED = "REFUTED"
    """A deterministic check failed. The answer is wrong."""

    INCONCLUSIVE = "INCONCLUSIVE"
    """A check was attempted and could not decide."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """Nothing machine-checkable was found. The expected case for prose."""


class VerificationMethod(StrEnum):
    SUBSTITUTION = "SUBSTITUTION"
    SYMBOLIC_EQUIVALENCE = "SYMBOLIC_EQUIVALENCE"
    NUMERIC_TOLERANCE = "NUMERIC_TOLERANCE"
    DIMENSIONAL = "DIMENSIONAL"
    ARITHMETIC = "ARITHMETIC"
    NONE = "NONE"


class VerificationResult(Frozen):
    """What was checked, how, and what it proves - stated exactly."""

    verdict: VerificationVerdict
    method: VerificationMethod = VerificationMethod.NONE
    detail: str = ""
    checked_claim: str | None = None
    """The specific claim that was checked. None when nothing was extractable."""

    @property
    def is_decisive(self) -> bool:
        return self.verdict in {
            VerificationVerdict.VERIFIED,
            VerificationVerdict.REFUTED,
        }

    @property
    def supports_answer(self) -> bool:
        return self.verdict is VerificationVerdict.VERIFIED


# --- homework task state ------------------------------------------------------


class TaskStage(StrEnum):
    """Where a homework task has got to. Persisted so "I don't understand
    step 3" resolves against the steps that were actually shown."""

    POSED = "POSED"
    HINTED = "HINTED"
    SOLVED = "SOLVED"
    CHECKED = "CHECKED"
    CLOSED = "CLOSED"


class HomeworkAction(StrEnum):
    HINT = "HINT"
    FULL_SOLUTION = "FULL_SOLUTION"
    EXPLAIN_STEP = "EXPLAIN_STEP"
    CHECK_MY_ANSWER = "CHECK_MY_ANSWER"
    ANALYSE_MISTAKE = "ANALYSE_MISTAKE"
    SIMPLER = "SIMPLER"
    DIAGRAM = "DIAGRAM"
    SIMILAR_PROBLEM = "SIMILAR_PROBLEM"
    HARDER = "HARDER"
    EASIER = "EASIER"


class SolutionStep(Frozen):
    number: int = Field(ge=1)
    text: str
    reason: str = ""


# --- visual artifacts ---------------------------------------------------------


class ArtifactKind(StrEnum):
    FUNCTION_PLOT = "FUNCTION_PLOT"
    GEOMETRY = "GEOMETRY"
    FREE_BODY = "FREE_BODY"
    CIRCUIT = "CIRCUIT"
    BLOCK_DIAGRAM = "BLOCK_DIAGRAM"
    PRINTABLE_PAPER = "PRINTABLE_PAPER"
    """A mock test rendered for printing. Built from `StudentQuestion` only, so
    the artifact cannot contain the key it was generated alongside."""


class ArtifactFormat(StrEnum):
    PNG = "PNG"
    SVG = "SVG"
    TIKZ = "TIKZ"
    TEXT = "TEXT"


class ArtifactRef(Frozen):
    """Metadata for a generated diagram. Bytes live in the BlobStore."""

    id: UUID
    kind: ArtifactKind
    artifact_format: ArtifactFormat
    blob_key: str
    sha256: str
    width: int = 0
    height: int = 0
    generated_by: str = "deterministic"
    """`deterministic` or `model_spec`. Records whether a model was involved in
    producing the *specification* - never the pixels."""


# --- assessment ---------------------------------------------------------------


class QuestionType(StrEnum):
    MCQ = "MCQ"
    TRUE_FALSE = "TRUE_FALSE"
    SHORT_ANSWER = "SHORT_ANSWER"
    NUMERIC = "NUMERIC"
    STRUCTURED = "STRUCTURED"

    @property
    def is_objective(self) -> bool:
        """Objective types grade deterministically - zero model calls."""
        return self in {
            QuestionType.MCQ,
            QuestionType.TRUE_FALSE,
            QuestionType.NUMERIC,
        }


class AssessmentKind(StrEnum):
    QUIZ = "QUIZ"
    MOCK_TEST = "MOCK_TEST"
    PRACTICE = "PRACTICE"


class AttemptState(StrEnum):
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    GRADED = "GRADED"


class QuestionSpec(Frozen):
    """A question WITH its answer key. Never sent to a student in test mode.

    `StudentQuestion` is the projection that omits the key - see
    `assessment/delivery.py`. The separation is structural, not a convention.
    """

    number: int = Field(ge=1)
    question_type: QuestionType
    prompt: str
    marks: int = Field(default=1, ge=1)
    topic: str = ""
    options: tuple[str, ...] = ()
    correct_option: int | None = None
    correct_boolean: bool | None = None
    correct_numeric: float | None = None
    numeric_unit: str | None = None
    numeric_tolerance: float = 0.01
    expected_answer: str | None = None
    rubric: str | None = None
    worked_solution: str = ""


class StudentQuestion(Frozen):
    """What a student receives. Structurally cannot carry an answer key -
    the fields do not exist on this type."""

    number: int
    question_type: QuestionType
    prompt: str
    marks: int
    options: tuple[str, ...] = ()


class QuestionResponse(Frozen):
    number: int
    chosen_option: int | None = None
    boolean_answer: bool | None = None
    numeric_answer: float | None = None
    numeric_unit: str | None = None
    text_answer: str | None = None


class GradedQuestion(Frozen):
    number: int
    awarded: float = Field(ge=0)
    possible: int = Field(ge=1)
    correct: bool
    feedback: str = ""
    graded_by: str = "deterministic"
    """`deterministic` or `model`. Makes the model-call count auditable."""

    needs_manual_review: bool = False
    evidence: str = ""


class GradeReport(Frozen):
    graded: tuple[GradedQuestion, ...]
    total_awarded: float
    total_possible: int
    model_calls: int = 0

    @property
    def percentage(self) -> float:
        if self.total_possible == 0:
            return 0.0
        return round(100.0 * self.total_awarded / self.total_possible, 1)

    @property
    def needs_review(self) -> bool:
        return any(g.needs_manual_review for g in self.graded)


# --- spaced repetition --------------------------------------------------------


class ReviewGrade(StrEnum):
    """SM-2 style recall quality, reduced to what a student can actually
    report honestly."""

    AGAIN = "AGAIN"
    HARD = "HARD"
    GOOD = "GOOD"
    EASY = "EASY"


class CardSchedule(Frozen):
    """Deterministic scheduling state. Never produced by a model."""

    repetitions: int = Field(default=0, ge=0)
    interval_days: int = Field(default=0, ge=0)
    ease_factor: float = Field(default=2.5, ge=1.3, le=3.0)
    due_on: date | None = None
    lapses: int = Field(default=0, ge=0)


# --- progress -----------------------------------------------------------------


class MasterySignal(StrEnum):
    """Honest bands. Not a percentage - a percentage from four attempts is
    fake precision dressed as measurement."""

    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    STRUGGLING = "STRUGGLING"
    DEVELOPING = "DEVELOPING"
    SECURE = "SECURE"


MIN_ATTEMPTS_FOR_SIGNAL = 5
"""Below this, the honest answer is INSUFFICIENT_EVIDENCE. A student with three
attempts does not have a mastery level, and inventing one misleads both them and
their tutor."""


class TopicProgress(Frozen):
    """Observed counters, plus a band that says how much they support."""

    topic: str
    attempts: int = Field(ge=0)
    correct: int = Field(ge=0)
    hints_used: int = Field(default=0, ge=0)
    last_seen_at: datetime | None = None

    @property
    def accuracy(self) -> float | None:
        """None below the evidence threshold - deliberately not 0.0, which
        would read as 'always wrong' rather than 'not enough data'."""
        if self.attempts < MIN_ATTEMPTS_FOR_SIGNAL:
            return None
        return round(self.correct / self.attempts, 3)

    @property
    def signal(self) -> MasterySignal:
        accuracy = self.accuracy
        if accuracy is None:
            return MasterySignal.INSUFFICIENT_EVIDENCE
        if accuracy < 0.5:
            return MasterySignal.STRUGGLING
        if accuracy < 0.8:
            return MasterySignal.DEVELOPING
        return MasterySignal.SECURE


class WeakTopic(Frozen):
    """A recommendation, with the evidence that produced it attached."""

    topic: str
    signal: MasterySignal
    attempts: int
    correct: int
    evidence: str
    """Plain-language justification, e.g. '3 of 9 correct over 9 attempts'."""
