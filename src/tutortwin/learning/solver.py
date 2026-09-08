"""The STEM solver pipeline and its verification policy.

The pipeline is:

    normalize -> detect subject -> state assumptions -> solve
    -> LOCAL deterministic verification -> confidence
    -> SELECTIVE second-model verification -> step-by-step response

**Local verification runs before the paid one, and can end the pipeline.** A
substitution that refutes an answer costs nothing and is certain; a second
frontier model costs money and returns another opinion. Running the expensive
check first would be paying for the weaker signal.

**Disagreement produces a qualified answer, never a coin flip.** When two models
differ on the final result there is no evidence about which is right, so the
student is told the result is contested. Silently returning the first one is
false certainty; silently returning the second is false certainty with an extra
invoice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from tutortwin.domain.budget import ExecutionBudgetDecision, VerifierMode
from tutortwin.domain.capabilities import CapabilityId, ConfidenceBand, Difficulty
from tutortwin.domain.learning import VerificationResult, VerificationVerdict
from tutortwin.learning.verification import (
    NumericClaim,
    VerifiableProblem,
    extract_numeric_claims,
    verify_answer,
)
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)


class SolverSubject(StrEnum):
    ALGEBRA = "ALGEBRA"
    CALCULUS = "CALCULUS"
    GEOMETRY = "GEOMETRY"
    PHYSICS = "PHYSICS"
    CHEMISTRY = "CHEMISTRY"
    BIOLOGY = "BIOLOGY"
    OTHER = "OTHER"


# Keyword detection, not a model call. The subject steers the prompt block and
# the local verifier; asking an LLM would add a call and a failure mode to a
# decision a word list settles.
_SUBJECT_TERMS: tuple[tuple[SolverSubject, tuple[str, ...]], ...] = (
    (
        SolverSubject.CALCULUS,
        ("derivative", "differentiate", "integral", "integrate", "limit", "dy/dx", "d/dx"),
    ),
    (
        SolverSubject.PHYSICS,
        (
            "velocity",
            "acceleration",
            "force",
            "newton",
            "momentum",
            "kinetic",
            "voltage",
            "current",
            "resistor",
            "wavelength",
            "friction",
            "projectile",
        ),
    ),
    (
        SolverSubject.CHEMISTRY,
        ("mole", "molar", "stoichiom", "reaction", "titration", "ph of", "oxidation"),
    ),
    (
        SolverSubject.BIOLOGY,
        ("cell", "mitosis", "enzyme", "photosynth", "chromosome", "genotype"),
    ),
    (
        SolverSubject.GEOMETRY,
        ("triangle", "circle", "polygon", "perimeter", "hypotenuse", "angle"),
    ),
    (
        SolverSubject.ALGEBRA,
        ("solve for", "equation", "quadratic", "factorise", "factorize", "simplify"),
    ),
)

_WHITESPACE = re.compile(r"\s+")

# OCR and phone keyboards emit typographic operators the expression parser would
# reject as illegal characters. Normalising them here is what lets a photographed
# worksheet verify exactly like a typed one.
_UNICODE_MATH = {
    "−": "-",  # true minus sign
    "–": "-",  # en dash
    "—": "-",  # em dash
    "×": "*",
    "÷": "/",
    "√": "sqrt",
    "²": "^2",
    "³": "^3",
    "≤": "<=",
    "≥": ">=",
}

MAX_PROBLEM_CHARS = 4000


@dataclass(frozen=True, slots=True)
class NormalizedProblem:
    """A problem statement in a shape the rest of the pipeline can rely on."""

    text: str
    subject: SolverSubject
    claims: tuple[NumericClaim, ...]
    assumptions: tuple[str, ...]
    has_units: bool


# Assumptions are stated so a student can challenge them. A wrong answer that
# names its assumption is teachable; one that hides it is not.
_STANDARD_ASSUMPTIONS: dict[SolverSubject, tuple[str, ...]] = {
    SolverSubject.PHYSICS: (
        "g = 9.81 m/s^2 unless the question states otherwise",
        "air resistance is neglected unless the question mentions it",
    ),
    SolverSubject.CHEMISTRY: ("standard temperature and pressure unless stated otherwise",),
    SolverSubject.ALGEBRA: ("the variable ranges over the real numbers",),
    SolverSubject.CALCULUS: ("functions are differentiable on the interval given",),
}

_UNIT_HINT = re.compile(
    r"\b\d+(?:\.\d+)?\s*(m|km|cm|mm|s|ms|kg|g|mg|n|j|w|v|a|hz|mol|l|ml|c|k)\b",
    re.IGNORECASE,
)


def detect_subject(text: str) -> SolverSubject:
    """First matching term wins, in specificity order."""
    lowered = text.lower()
    for subject, terms in _SUBJECT_TERMS:
        if any(term in lowered for term in terms):
            return subject
    return SolverSubject.OTHER


def normalize_problem(raw: str) -> NormalizedProblem:
    """Canonicalise a problem statement. Deterministic, no model call."""
    text = raw[:MAX_PROBLEM_CHARS]
    for source, target in _UNICODE_MATH.items():
        text = text.replace(source, target)
    text = _WHITESPACE.sub(" ", text).strip()

    subject = detect_subject(text)
    return NormalizedProblem(
        text=text,
        subject=subject,
        claims=extract_numeric_claims(text),
        assumptions=_STANDARD_ASSUMPTIONS.get(subject, ()),
        has_units=bool(_UNIT_HINT.search(text)),
    )


# --- verification policy ------------------------------------------------------


class VerificationTier(StrEnum):
    """What was actually done to check an answer. Reported, not inferred."""

    NONE = "NONE"
    LOCAL_ONLY = "LOCAL_ONLY"
    LOCAL_THEN_MODEL = "LOCAL_THEN_MODEL"
    MODEL_ONLY = "MODEL_ONLY"


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    """The decision, with the reason attached so a test can assert on it."""

    run_local: bool
    run_second_model: bool
    tier: VerificationTier
    reason: str

    @property
    def extra_model_calls(self) -> int:
        return 1 if self.run_second_model else 0


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """When a second model is worth its price.

    Not every STEM question earns one. "What is 7 x 8" and "explain
    photosynthesis" are answered once; a low-confidence multi-step derivation is
    worth checking.
    """

    stem_capabilities: frozenset[CapabilityId] = field(
        default_factory=lambda: frozenset(
            {
                CapabilityId.MATH,
                CapabilityId.PHYSICS,
                CapabilityId.CHEMISTRY,
                CapabilityId.HOMEWORK_SOLVE,
            }
        )
    )

    def plan(
        self,
        *,
        capability: CapabilityId,
        difficulty: Difficulty,
        confidence: ConfidenceBand,
        decision: ExecutionBudgetDecision,
        local: VerificationResult | None,
    ) -> VerificationPlan:
        is_stem = capability in self.stem_capabilities

        if local is not None and local.verdict is VerificationVerdict.REFUTED:
            # Certainty, free. A second model can only agree with the maths or be
            # wrong about it, and either way the answer already needs redoing.
            return VerificationPlan(
                run_local=True,
                run_second_model=False,
                tier=VerificationTier.LOCAL_ONLY,
                reason="local_check_refuted",
            )
        if local is not None and local.verdict is VerificationVerdict.VERIFIED:
            return VerificationPlan(
                run_local=True,
                run_second_model=False,
                tier=VerificationTier.LOCAL_ONLY,
                reason="local_check_verified",
            )

        if decision.verifier_mode is VerifierMode.NONE or decision.verifier_alias is None:
            return VerificationPlan(
                run_local=is_stem,
                run_second_model=False,
                tier=VerificationTier.LOCAL_ONLY if is_stem else VerificationTier.NONE,
                reason="plan_has_no_verifier",
            )

        if decision.verifier_mode is VerifierMode.ALWAYS:
            return VerificationPlan(
                run_local=is_stem,
                run_second_model=True,
                tier=VerificationTier.LOCAL_THEN_MODEL if is_stem else VerificationTier.MODEL_ONLY,
                reason="verifier_mode_always",
            )

        # ON_LOW_CONFIDENCE. A cheap, easy, confident answer is not re-checked -
        # that is the difference between a cost-aware system and a two-model one.
        if not is_stem:
            return VerificationPlan(
                run_local=False,
                run_second_model=False,
                tier=VerificationTier.NONE,
                reason="not_a_stem_capability",
            )
        if difficulty is Difficulty.SIMPLE:
            return VerificationPlan(
                run_local=True,
                run_second_model=False,
                tier=VerificationTier.LOCAL_ONLY,
                reason="simple_question_local_check_only",
            )
        if confidence is ConfidenceBand.LOW:
            return VerificationPlan(
                run_local=True,
                run_second_model=True,
                tier=VerificationTier.LOCAL_THEN_MODEL,
                reason="low_confidence_hard_stem",
            )
        return VerificationPlan(
            run_local=True,
            run_second_model=False,
            tier=VerificationTier.LOCAL_ONLY,
            reason="confidence_sufficient",
        )


def run_local_verification(
    answer_text: str, problem: VerifiableProblem | None
) -> VerificationResult:
    """Local check, or an honest NOT_APPLICABLE.

    A problem with nothing machine-checkable in it is the common case, and
    labelling that `VERIFIED` would be a lie the whole engine rests on.
    """
    if problem is None:
        return VerificationResult(
            verdict=VerificationVerdict.NOT_APPLICABLE,
            detail="no machine-checkable form was extracted from the problem",
        )
    return verify_answer(answer_text, problem)


# --- cross-model comparison ---------------------------------------------------

_ASSUMPTION_LINE = re.compile(r"^\s*(?:assumption|assume|given)\b[:\-]?\s*(.+)$", re.IGNORECASE)
_FINAL_LINE = re.compile(r"(?:final answer|answer|result|therefore)\s*[:=]\s*(.+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CrossModelComparison:
    """What the two answers actually agreed on, field by field."""

    final_agrees: bool | None
    """`None` when neither answer stated a comparable final result."""

    shared_assumptions: tuple[str, ...]
    conflicting_assumptions: tuple[str, ...]
    conflicting_quantities: tuple[str, ...]

    @property
    def disagrees(self) -> bool:
        return (
            self.final_agrees is False
            or bool(self.conflicting_assumptions)
            or bool(self.conflicting_quantities)
        )


def _final_result(text: str) -> str | None:
    matches = _FINAL_LINE.findall(text)
    return matches[-1].strip().rstrip(".").lower() if matches else None


def _assumptions(text: str) -> set[str]:
    found: set[str] = set()
    for line in text.splitlines():
        match = _ASSUMPTION_LINE.match(line)
        if match:
            found.add(_WHITESPACE.sub(" ", match.group(1)).strip().rstrip(".").lower())
    return found


def compare_solutions(primary: str, secondary: str) -> CrossModelComparison:
    """Compare final result, assumptions and critical intermediate quantities.

    Comparing whole texts would flag every rewording as a disagreement, which is
    both useless and expensive to act on. These three fields are where a
    difference means the answers genuinely differ.
    """
    left_final, right_final = _final_result(primary), _final_result(secondary)
    final_agrees: bool | None = None
    if left_final is not None and right_final is not None:
        final_agrees = _same_result(left_final, right_final)

    left_assumptions, right_assumptions = _assumptions(primary), _assumptions(secondary)
    shared = left_assumptions & right_assumptions

    # Only named quantities are comparable. An unnamed final number in one
    # answer and an unnamed one in the other could be measuring different things,
    # and reporting that as a conflict would flag every correct pair.
    left_claims = {c.variable: c.value for c in extract_numeric_claims(primary) if c.variable}
    right_claims = {c.variable: c.value for c in extract_numeric_claims(secondary) if c.variable}
    conflicts = tuple(
        sorted(
            name
            for name, value in left_claims.items()
            if name in right_claims and not _close(value, right_claims[name])
        )
    )

    return CrossModelComparison(
        final_agrees=final_agrees,
        shared_assumptions=tuple(sorted(shared)),
        conflicting_assumptions=tuple(sorted((left_assumptions | right_assumptions) - shared)),
        conflicting_quantities=conflicts,
    )


def _close(left: float, right: float, *, tolerance: float = 1e-6) -> bool:
    scale = max(1.0, abs(left), abs(right))
    return abs(left - right) <= tolerance * scale


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _same_result(left: str, right: str) -> bool:
    """Numbers first: `x = 6` and `6` are the same answer written differently."""
    left_numbers = _NUMBER.findall(left)
    right_numbers = _NUMBER.findall(right)
    if left_numbers and right_numbers:
        return len(left_numbers) == len(right_numbers) and all(
            _close(float(a), float(b)) for a, b in zip(left_numbers, right_numbers, strict=False)
        )
    return _WHITESPACE.sub(" ", left) == _WHITESPACE.sub(" ", right)


QUALIFIED_PREFIX = (
    "I checked this answer a second way and the two checks did not agree, so treat "
    "the result below as provisional and work through the steps yourself:"
)


def qualify(answer: str, comparison: CrossModelComparison) -> str:
    """Prefix a contested answer rather than picking a winner.

    There is no evidence for choosing between two disagreeing models, so choosing
    one and presenting it plainly manufactures confidence that does not exist.
    """
    if not comparison.disagrees:
        return answer
    detail: list[str] = []
    if comparison.final_agrees is False:
        detail.append("the final results differ")
    if comparison.conflicting_quantities:
        detail.append("intermediate values differ: " + ", ".join(comparison.conflicting_quantities))
    if comparison.conflicting_assumptions:
        detail.append("the two checks assumed different things")
    return f"{QUALIFIED_PREFIX} ({'; '.join(detail)})\n\n{answer}"


__all__ = [
    "CrossModelComparison",
    "NormalizedProblem",
    "SolverSubject",
    "VerificationPlan",
    "VerificationPolicy",
    "VerificationTier",
    "compare_solutions",
    "detect_subject",
    "normalize_problem",
    "qualify",
    "run_local_verification",
]
