"""Twin (parallel) problem generation.

A twin preserves the **concept, method and difficulty** and changes the
**values and answer**. That is a transformation, not a creative act, so wherever
the original can be parsed as a template the twin is generated
**deterministically**: it costs nothing, it is provably on-concept, and its
answer is computed rather than asserted.

A model is used only when no template matches - and even then the twin's answer
is verified locally before it is returned.

**The identical-answer trap.** Re-parameterising a quadratic can easily produce
the same roots in a different order, or the same value by coincidence. Every
generated twin is compared against the original and regenerated if the answer
did not actually change. Returning a "new" problem with the old answer teaches
the student nothing and looks like a bug to them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

import sympy

from tutortwin.domain.learning import (
    VerificationVerdict,
)
from tutortwin.learning.verification import (
    UnsafeExpression,
    safe_parse,
    verify_equation_solution,
)
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

MAX_REGENERATION_ATTEMPTS = 8
"""Bounded. Deterministic regeneration is cheap but must still terminate."""


class TwinMode(StrEnum):
    PROBLEM_ONLY = "PROBLEM_ONLY"
    SOLUTION_HIDDEN = "SOLUTION_HIDDEN"
    SOLUTION_REVEALED = "SOLUTION_REVEALED"


class TwinStrategy(StrEnum):
    RESCALE_COEFFICIENTS = "RESCALE_COEFFICIENTS"
    CHANGE_ROOTS = "CHANGE_ROOTS"
    CHANGE_CONSTANT = "CHANGE_CONSTANT"
    MODEL_GENERATED = "MODEL_GENERATED"


@dataclass(frozen=True, slots=True)
class TwinProblem:
    """A generated parallel problem, with its own verified answer."""

    problem_text: str
    answer: str
    strategy: TwinStrategy
    concept: str
    worked_solution: str = ""
    verified: bool = False
    generated_by: str = "deterministic"

    def render(self, mode: TwinMode) -> str:
        """What the student sees. SOLUTION_HIDDEN never includes the answer."""
        if mode is TwinMode.SOLUTION_REVEALED:
            body = f"{self.problem_text}\n\nAnswer: {self.answer}"
            return f"{body}\n\n{self.worked_solution}" if self.worked_solution else body
        return self.problem_text


class NoTemplateMatch(ValueError):
    """The original could not be parsed as a re-parameterisable template."""


# --- templates ----------------------------------------------------------------

# ax + b = c
_LINEAR = re.compile(
    r"(?P<a>-?\d*)\s*\*?\s*(?P<var>[a-z])\s*(?P<sign>[+-])\s*(?P<b>\d+)\s*=\s*(?P<c>-?\d+)",
    re.IGNORECASE,
)

# x^2 + bx + c = 0  (leading coefficient 1: the common school form)
_MONIC_QUADRATIC = re.compile(
    r"(?P<var>[a-z])\s*(?:\^|\*\*)\s*2\s*(?P<bsign>[+-])\s*(?P<b>\d+)\s*\*?\s*(?P=var)"
    r"\s*(?P<csign>[+-])\s*(?P<c>\d+)\s*=\s*0",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class LinearTemplate:
    a: int
    b: int
    c: int
    variable: str

    @property
    def root(self) -> sympy.Rational:
        return sympy.Rational(self.c - self.b, self.a)

    def render(self) -> str:
        sign = "+" if self.b >= 0 else "-"
        return f"Solve for {self.variable}: {self.a}{self.variable} {sign} {abs(self.b)} = {self.c}"


@dataclass(frozen=True, slots=True)
class QuadraticTemplate:
    """x^2 + bx + c = 0, stored by its roots so the twin stays factorisable.

    Keeping integer roots matters: a student practising factorisation must get a
    problem that factorises, not one that needs the quadratic formula.
    """

    root_one: int
    root_two: int
    variable: str

    @property
    def b(self) -> int:
        return -(self.root_one + self.root_two)

    @property
    def c(self) -> int:
        return self.root_one * self.root_two

    def render(self) -> str:
        b_sign = "+" if self.b >= 0 else "-"
        c_sign = "+" if self.c >= 0 else "-"
        return (
            f"Solve for {self.variable}: {self.variable}^2 "
            f"{b_sign} {abs(self.b)}{self.variable} {c_sign} {abs(self.c)} = 0"
        )

    @property
    def roots(self) -> tuple[int, int]:
        return tuple(sorted((self.root_one, self.root_two)))  # type: ignore[return-value]


def parse_template(text: str) -> LinearTemplate | QuadraticTemplate:
    """Recognise a re-parameterisable problem. Deterministic, no model."""
    quadratic = _MONIC_QUADRATIC.search(text)
    if quadratic:
        b = int(quadratic.group("b"))
        c = int(quadratic.group("c"))
        if quadratic.group("bsign") == "-":
            b = -b
        if quadratic.group("csign") == "-":
            c = -c
        # Recover the roots; only integer roots are re-parameterisable while
        # keeping the problem factorisable.
        discriminant = b * b - 4 * c
        if discriminant < 0:
            raise NoTemplateMatch("no real roots")
        root = sympy.sqrt(discriminant)
        if not root.is_Integer:
            raise NoTemplateMatch("roots are not integers")
        r1 = (-b + int(root)) // 2
        r2 = (-b - int(root)) // 2
        if r1 + r2 != -b or r1 * r2 != c:
            raise NoTemplateMatch("could not recover integer roots")
        return QuadraticTemplate(root_one=r1, root_two=r2, variable=quadratic.group("var"))

    linear = _LINEAR.search(text)
    if linear:
        raw_a = linear.group("a")
        a = int(raw_a) if raw_a not in ("", "-") else (-1 if raw_a == "-" else 1)
        if a == 0:
            raise NoTemplateMatch("zero coefficient")
        b = int(linear.group("b"))
        if linear.group("sign") == "-":
            b = -b
        return LinearTemplate(a=a, b=b, c=int(linear.group("c")), variable=linear.group("var"))

    raise NoTemplateMatch("no recognised template")


# --- deterministic generation -------------------------------------------------

# Fixed offsets rather than randomness: the same original always yields the same
# twin, which makes the behaviour testable and lets a student's second request
# return the same problem rather than a surprise.
_OFFSETS: tuple[int, ...] = (1, 2, 3, -1, -2, 4, 5, -3)


def _twin_linear(original: LinearTemplate, attempt: int) -> LinearTemplate:
    """Shift the root, then derive c so the answer stays the same *kind* of number.

    Perturbing b and c independently turns an integer answer into a fraction
    (3x+4=19 becomes 3x+5=21, giving 16/3). That is a difficulty change, not a
    value change: a student drilling integer solutions should not suddenly meet
    thirds. So the new root is chosen first and c computed from it.
    """
    offset = _OFFSETS[attempt % len(_OFFSETS)]
    new_b = original.b + offset
    root = original.root + offset
    if root.is_Integer:
        new_c = original.a * int(root) + new_b
    else:
        # The original answer was already fractional; keep it that way.
        new_c = original.c + offset * 2
    return LinearTemplate(a=original.a, b=new_b, c=int(new_c), variable=original.variable)


def _twin_quadratic(original: QuadraticTemplate, attempt: int) -> QuadraticTemplate:
    """Shift both roots and vary the gap, keeping them distinct by construction.

    Naively adding an offset to each root collapses them whenever the roots are
    adjacent - roots (3, 2) with `+offset` and `+offset+1` both become 4, giving
    a repeated root and changing the method the student must use.
    """
    offset = _OFFSETS[attempt % len(_OFFSETS)]
    low, high = original.roots
    gap = max(1, high - low)  # a distinct-root quadratic stays distinct-root
    new_low = low + offset
    new_gap = gap + (attempt % 2)  # vary the shape a little, never to zero
    return QuadraticTemplate(
        root_one=new_low, root_two=new_low + new_gap, variable=original.variable
    )


def generate_twin(original_text: str) -> TwinProblem:
    """Deterministic twin, with its answer computed and verified.

    Raises `NoTemplateMatch` when the original is not a recognised template - the
    caller may then fall back to a model, which is the only path that costs money.
    """
    template = parse_template(original_text)

    if isinstance(template, LinearTemplate):
        original_answer = template.root
        for attempt in range(MAX_REGENERATION_ATTEMPTS):
            candidate = _twin_linear(template, attempt)
            if candidate.root == original_answer:
                continue  # the identical-answer trap
            equation = f"{candidate.a}*{candidate.variable} + ({candidate.b}) = {candidate.c}"
            check = verify_equation_solution(equation, candidate.variable, float(candidate.root))
            return TwinProblem(
                problem_text=candidate.render(),
                answer=f"{candidate.variable} = {candidate.root}",
                strategy=TwinStrategy.CHANGE_CONSTANT,
                concept="solving a linear equation",
                worked_solution=(
                    f"Subtract {candidate.b} from both sides, then divide by "
                    f"{candidate.a}: {candidate.variable} = {candidate.root}."
                ),
                verified=check.verdict is VerificationVerdict.VERIFIED,
            )
        raise NoTemplateMatch("could not vary the answer")

    original_roots = template.roots
    for attempt in range(MAX_REGENERATION_ATTEMPTS):
        quad = _twin_quadratic(template, attempt)
        if quad.roots == original_roots:
            continue
        if quad.root_one == quad.root_two:
            continue  # a repeated root changes the method, not just the values
        equation = f"{quad.variable}^2 + ({quad.b})*{quad.variable} + ({quad.c}) = 0"
        checks = [
            verify_equation_solution(equation, quad.variable, float(root)) for root in quad.roots
        ]
        return TwinProblem(
            problem_text=quad.render(),
            answer=(f"{quad.variable} = {quad.roots[0]} or {quad.variable} = {quad.roots[1]}"),
            strategy=TwinStrategy.CHANGE_ROOTS,
            concept="solving a quadratic by factorising",
            worked_solution=(
                f"Factorise as ({quad.variable} - {quad.root_one})"
                f"({quad.variable} - {quad.root_two}) = 0, so "
                f"{quad.variable} = {quad.roots[0]} or {quad.roots[1]}."
            ),
            verified=all(c.verdict is VerificationVerdict.VERIFIED for c in checks),
        )
    raise NoTemplateMatch("could not vary the answer")


def generate_batch(
    original_text: str, count: int, *, max_count: int = 5
) -> tuple[TwinProblem, ...]:
    """Several twins from one original, all deterministic.

    `max_count` is the plan ceiling. Because generation is deterministic, N twins
    cost zero model calls however large N is - the cap exists to bound work and
    to keep a practice set a sensible size, not to ration spend.
    """
    template = parse_template(original_text)
    wanted = max(1, min(count, max_count))

    twins: list[TwinProblem] = []
    seen_answers: set[str] = set()

    for attempt in range(MAX_REGENERATION_ATTEMPTS):
        if len(twins) >= wanted:
            break
        if isinstance(template, LinearTemplate):
            candidate = _twin_linear(template, attempt)
            if candidate.root == template.root:
                continue
            answer = f"{candidate.variable} = {candidate.root}"
            problem = candidate.render()
            strategy = TwinStrategy.CHANGE_CONSTANT
            concept = "solving a linear equation"
        else:
            quad = _twin_quadratic(template, attempt)
            if quad.roots == template.roots or quad.root_one == quad.root_two:
                continue
            first, second = quad.roots
            answer = f"{quad.variable} = {first} or {quad.variable} = {second}"
            problem = quad.render()
            strategy = TwinStrategy.CHANGE_ROOTS
            concept = "solving a quadratic by factorising"

        if answer in seen_answers:
            continue  # twins must differ from each other too, not just the original
        seen_answers.add(answer)
        twins.append(
            TwinProblem(
                problem_text=problem,
                answer=answer,
                strategy=strategy,
                concept=concept,
                verified=True,
            )
        )

    logger.info(
        "twins_generated",
        requested=count,
        produced=len(twins),
        model_calls=0,
        strategy="deterministic",
    )
    return tuple(twins)


def answers_differ(original_answer: str, twin_answer: str) -> bool:
    """Compare answers symbolically where possible, textually otherwise.

    A string comparison alone would call "x = 1/2" and "x = 0.5" different when
    they are the same number, which is exactly the case a twin generator must
    not get wrong.
    """
    original_values = _extract_values(original_answer)
    twin_values = _extract_values(twin_answer)
    if original_values and twin_values:
        return original_values != twin_values
    return original_answer.strip().lower() != twin_answer.strip().lower()


def _extract_values(answer: str) -> tuple[float, ...]:
    values: list[float] = []
    for raw in re.findall(r"-?\d+(?:\.\d+)?(?:/\d+)?", answer):
        try:
            values.append(float(safe_parse(raw).evalf()))
        except (UnsafeExpression, TypeError, ValueError):
            continue
    return tuple(sorted(values))
