"""Capability registry, pedagogy modes and the confidence contract.

The capability IDs are the stable vocabulary of the whole product: routing,
budget policy, prompts, usage records and admin config all key off them. The
enum is complete from Phase 02 even though only the text-capable subset
executes here - adding a member later would silently change the meaning of
stored rows, so the schema is fixed up front and execution catches up.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CapabilityId(StrEnum):
    GENERAL_TUTORING = "GENERAL_TUTORING"
    EXPLAIN_CONCEPT = "EXPLAIN_CONCEPT"
    HOMEWORK_SOLVE = "HOMEWORK_SOLVE"
    MATH = "MATH"
    PHYSICS = "PHYSICS"
    CHEMISTRY = "CHEMISTRY"
    BIOLOGY = "BIOLOGY"
    CODING = "CODING"
    WRITING_FEEDBACK = "WRITING_FEEDBACK"
    LANGUAGE_HELP = "LANGUAGE_HELP"
    ANSWER_CHECK = "ANSWER_CHECK"
    GRADE_WORK = "GRADE_WORK"
    PRACTICE_GENERATION = "PRACTICE_GENERATION"
    TWIN_PROBLEM = "TWIN_PROBLEM"
    FLASHCARDS = "FLASHCARDS"
    QUIZ = "QUIZ"
    MOCK_TEST = "MOCK_TEST"
    DOCUMENT_QA = "DOCUMENT_QA"
    IMAGE_QA = "IMAGE_QA"
    REVISION = "REVISION"
    STUDY_PLAN = "STUDY_PLAN"
    RESEARCH_HELP = "RESEARCH_HELP"
    UNKNOWN = "UNKNOWN"


# Executable in Phase 02. The rest are registered but routed to a capability-not-
# available response, so a stored row never implies work that did not happen.
TEXT_CAPABILITIES: frozenset[CapabilityId] = frozenset(
    {
        CapabilityId.GENERAL_TUTORING,
        CapabilityId.EXPLAIN_CONCEPT,
        CapabilityId.HOMEWORK_SOLVE,
        CapabilityId.MATH,
        CapabilityId.PHYSICS,
        CapabilityId.CHEMISTRY,
        CapabilityId.BIOLOGY,
        CapabilityId.CODING,
        CapabilityId.WRITING_FEEDBACK,
        CapabilityId.LANGUAGE_HELP,
        CapabilityId.ANSWER_CHECK,
    }
)

# Requires media input; Phase 03 territory.
MEDIA_CAPABILITIES: frozenset[CapabilityId] = frozenset(
    {CapabilityId.DOCUMENT_QA, CapabilityId.IMAGE_QA}
)

# STEM capabilities carry a higher wrong-answer cost, so they are the ones
# eligible for second-model verification when confidence is low.
STEM_CAPABILITIES: frozenset[CapabilityId] = frozenset(
    {
        CapabilityId.MATH,
        CapabilityId.PHYSICS,
        CapabilityId.CHEMISTRY,
        CapabilityId.HOMEWORK_SOLVE,
    }
)


class Difficulty(StrEnum):
    """Deterministic difficulty estimate. Drives model tier selection."""

    SIMPLE = "SIMPLE"
    MODERATE = "MODERATE"
    ADVANCED = "ADVANCED"


class PedagogyMode(StrEnum):
    GUIDED = "GUIDED"
    HINT_FIRST = "HINT_FIRST"
    STEP_BY_STEP = "STEP_BY_STEP"
    ANSWER_AND_EXPLAIN = "ANSWER_AND_EXPLAIN"
    SOCRATIC = "SOCRATIC"
    EXAM_REVISION = "EXAM_REVISION"


class ConfidenceBand(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class NextAction(StrEnum):
    """What the tutor suggests the student does next."""

    AWAIT_STUDENT = "AWAIT_STUDENT"
    OFFER_HINT = "OFFER_HINT"
    OFFER_NEXT_STEP = "OFFER_NEXT_STEP"
    OFFER_PRACTICE = "OFFER_PRACTICE"
    ASK_CLARIFICATION = "ASK_CLARIFICATION"


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityResult(Frozen):
    """The contract every capability returns.

    `confidence` is a band derived from deterministic signals, never a number the
    model claimed about itself. `signals` records what produced the band so a low
    confidence result is explainable rather than mysterious.
    """

    capability: CapabilityId
    answer_text: str
    confidence: ConfidenceBand
    signals: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    verification_recommended: bool = False
    next_action: NextAction = NextAction.AWAIT_STUDENT
    citations: tuple[str, ...] = ()
    pedagogy_mode: PedagogyMode = PedagogyMode.GUIDED


class IntentDecision(Frozen):
    """Why the router chose a capability. Auditable, and asserted in tests."""

    capability: CapabilityId
    difficulty: Difficulty
    reason: str = Field(min_length=1)
    used_model: bool = False
    """True only when a cheap classification call was actually required."""

    is_follow_up: bool = False
    requested_mode: PedagogyMode | None = None
    """Set when the student explicitly asked for a different explanation depth."""
