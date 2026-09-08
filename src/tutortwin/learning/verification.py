"""Local deterministic verification of STEM answers.

This runs after the model answers and **before** any second model is consulted.
A deterministic check that says REFUTED costs nothing and is certain; a second
frontier model costs money and is merely another opinion.

**Honesty is the design constraint.** `NOT_APPLICABLE` is the common outcome and
is not a failure: most tutoring answers are prose and nothing in them is
machine-checkable. Reporting VERIFIED for an answer that was never actually
checked would be worse than not checking at all, because it would launder a
guess into a guarantee.

**Parsing untrusted text is the security boundary.** `sympy.sympify()` evaluates
its input: `sympify("__import__('os').getcwd()")` returns the working directory.
Model and student output both reach this module, so parsing uses two layers:

1. a **character and pattern filter** on the raw string, because a name
   whitelist alone does not stop `().__class__.__bases__` from reaching
   `object.__subclasses__`
2. a **whitelisted namespace**, so only mathematical names resolve

Both were verified against real escape attempts before this module was written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sympy
from sympy.parsing.sympy_parser import convert_xor, parse_expr, standard_transformations

from tutortwin.domain.learning import (
    VerificationMethod,
    VerificationResult,
    VerificationVerdict,
)
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

MAX_EXPRESSION_CHARS = 500
"""Bounds parse time. A legitimate school-level expression is far shorter, and an
unbounded one is how a parser is turned into a denial of service."""

# Layer 1: only characters a mathematical expression needs. Quotes, brackets and
# attribute dots are absent by construction, which is what closes the
# `().__class__` escape that a namespace whitelist leaves open.
_ALLOWED_CHARS = re.compile(r"^[0-9a-zA-Z_+\-*/^().,=<>!\s]*$")
_ATTRIBUTE_ACCESS = re.compile(r"[)\]\"']\s*\.|\.\s*[A-Za-z_]")

# Exponent guard. `9**9**9` passes every character check yet never returns:
# measured here, `2**1000` parses in 0.6 ms while `9**9**9` (9^387420489) hangs
# the thread indefinitely. Evaluation happens during parsing, so a post-parse
# complexity check is unreachable - the guard has to run on the raw string.
MAX_EXPONENT = 1000
"""2**1000 is a 302-digit number computed in under a millisecond; beyond this the
result stops being arithmetic a student could have meant."""

_EXPONENT_OP = re.compile(r"\*\*|\^")
_CHAINED_EXPONENT = re.compile(r"(?:\*\*|\^)\s*[\w.()]*?\s*(?:\*\*|\^)")
_NUMERIC_EXPONENT = re.compile(r"(?:\*\*|\^)\s*\(?\s*(\d+)")

# Layer 2: the only names that resolve during parsing. `Symbol` is required for
# `auto_symbol` to turn an undefined name into a symbol rather than a NameError.
_SAFE_NAMES: dict[str, object] = {
    name: getattr(sympy, name)
    for name in (
        "sin cos tan asin acos atan atan2 sinh cosh tanh exp log sqrt Abs "
        "pi E oo Eq Symbol Integer Float Rational factorial"
    ).split()
    if hasattr(sympy, name)
}
_SAFE_NAMES["ln"] = sympy.log

# Students write `x^2` for exponentiation far more often than they mean XOR, and
# without this SymPy reads `^` as bitwise xor and fails on symbols.
_TRANSFORMS = (*standard_transformations, convert_xor)


class UnsafeExpression(ValueError):
    """The input was rejected before parsing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def assert_parseable(text: str) -> None:
    """Layer 1. Raises `UnsafeExpression` rather than parsing hostile input."""
    if len(text) > MAX_EXPRESSION_CHARS:
        raise UnsafeExpression("too_long")
    if not _ALLOWED_CHARS.match(text):
        raise UnsafeExpression("illegal_character")
    if "__" in text:
        raise UnsafeExpression("dunder")
    if _ATTRIBUTE_ACCESS.search(text):
        raise UnsafeExpression("attribute_access")

    # Exponent bomb. Chained exponentiation is rejected outright: no school-level
    # expression needs `a**b**c`, and it is the shortest route to an unbounded
    # computation.
    if _CHAINED_EXPONENT.search(text):
        raise UnsafeExpression("chained_exponent")
    for literal in _NUMERIC_EXPONENT.findall(text):
        if int(literal) > MAX_EXPONENT:
            raise UnsafeExpression("exponent_too_large")


def safe_parse(text: str) -> sympy.Expr:
    """Parse a mathematical expression from untrusted text.

    Both layers apply. Never call `sympy.sympify` on model or student output
    anywhere else in the codebase.
    """
    cleaned = text.strip()
    assert_parseable(cleaned)
    try:
        expression = parse_expr(
            cleaned,
            transformations=_TRANSFORMS,
            global_dict=dict(_SAFE_NAMES),
            evaluate=True,
        )
    except Exception as exc:  # noqa: BLE001 - any parse failure is a rejection
        raise UnsafeExpression(f"parse_failed:{type(exc).__name__}") from exc
    if not isinstance(expression, sympy.Basic):
        raise UnsafeExpression("not_an_expression")
    return expression


# --- claim extraction ---------------------------------------------------------

# A checkable claim in prose almost always looks like "x = 3" or "= 42 m/s".
_ASSIGNMENT = re.compile(r"\b([a-zA-Z][a-zA-Z0-9_]{0,15})\s*=\s*(-?\d+(?:\.\d+)?(?:/\d+)?)\b")
_FINAL_NUMBER = re.compile(
    r"(?:answer|result|equals|therefore|so)\b[^.\n]{0,40}?"
    r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z/·^0-9]{0,12})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NumericClaim:
    value: float
    unit: str | None = None
    variable: str | None = None


def extract_numeric_claims(text: str) -> tuple[NumericClaim, ...]:
    """Pull checkable claims out of prose. Deterministic, no model.

    Deliberately conservative: it only recognises explicit assignment and
    explicitly-flagged final answers. A number appearing mid-sentence is not
    treated as a claim, because checking the wrong number and reporting VERIFIED
    is worse than reporting NOT_APPLICABLE.
    """
    claims: list[NumericClaim] = []

    for variable, raw in _ASSIGNMENT.findall(text):
        try:
            value = float(sympy.Rational(raw)) if "/" in raw else float(raw)
        except (ValueError, TypeError, ZeroDivisionError):
            continue
        claims.append(NumericClaim(value=value, variable=variable))

    for raw, unit in _FINAL_NUMBER.findall(text):
        try:
            value = float(raw)
        except ValueError:
            continue
        claims.append(NumericClaim(value=value, unit=unit.strip() or None))

    return tuple(claims)


# --- verification strategies --------------------------------------------------


def verify_equation_solution(
    equation: str, variable: str, claimed_value: float, *, tolerance: float = 1e-6
) -> VerificationResult:
    """Substitute the claimed root back into the equation.

    The strongest check available: substitution either yields zero or it does
    not, and no model opinion changes that.
    """
    try:
        left, _, right = equation.partition("=")
        expression = safe_parse(f"({left})-({right})") if right.strip() else safe_parse(left)
        symbol = sympy.Symbol(variable)
        residual = complex(expression.subs(symbol, claimed_value))
    except UnsafeExpression as exc:
        return VerificationResult(
            verdict=VerificationVerdict.NOT_APPLICABLE,
            method=VerificationMethod.SUBSTITUTION,
            detail=f"could not parse safely ({exc.reason})",
        )
    except (TypeError, ValueError, ZeroDivisionError, AttributeError) as exc:
        return VerificationResult(
            verdict=VerificationVerdict.INCONCLUSIVE,
            method=VerificationMethod.SUBSTITUTION,
            detail=type(exc).__name__,
        )

    claim = f"{variable} = {claimed_value} satisfies {equation}"
    if abs(residual) <= tolerance:
        return VerificationResult(
            verdict=VerificationVerdict.VERIFIED,
            method=VerificationMethod.SUBSTITUTION,
            detail=f"residual {abs(residual):.2e} within {tolerance:g}",
            checked_claim=claim,
        )
    return VerificationResult(
        verdict=VerificationVerdict.REFUTED,
        method=VerificationMethod.SUBSTITUTION,
        detail=f"residual {abs(residual):.4g} exceeds {tolerance:g}",
        checked_claim=claim,
    )


def verify_symbolic_equivalence(left: str, right: str) -> VerificationResult:
    """Are two expressions the same? `simplify(a - b) == 0`.

    Answers "is (x+1)^2 the same as x^2+2x+1" without a model.
    """
    try:
        difference = sympy.simplify(safe_parse(left) - safe_parse(right))
    except UnsafeExpression as exc:
        return VerificationResult(
            verdict=VerificationVerdict.NOT_APPLICABLE,
            method=VerificationMethod.SYMBOLIC_EQUIVALENCE,
            detail=f"could not parse safely ({exc.reason})",
        )
    except (TypeError, ValueError, AttributeError, RecursionError) as exc:
        return VerificationResult(
            verdict=VerificationVerdict.INCONCLUSIVE,
            method=VerificationMethod.SYMBOLIC_EQUIVALENCE,
            detail=type(exc).__name__,
        )

    claim = f"{left} == {right}"
    if difference == 0:
        return VerificationResult(
            verdict=VerificationVerdict.VERIFIED,
            method=VerificationMethod.SYMBOLIC_EQUIVALENCE,
            detail="difference simplifies to zero",
            checked_claim=claim,
        )
    # simplify() failing to reach zero is not proof of inequality - it may just
    # not have found the route. Saying REFUTED here would be false certainty.
    if difference.free_symbols:
        return VerificationResult(
            verdict=VerificationVerdict.INCONCLUSIVE,
            method=VerificationMethod.SYMBOLIC_EQUIVALENCE,
            detail=f"difference did not reduce: {difference}",
            checked_claim=claim,
        )
    return VerificationResult(
        verdict=VerificationVerdict.REFUTED,
        method=VerificationMethod.SYMBOLIC_EQUIVALENCE,
        detail=f"difference is {difference}",
        checked_claim=claim,
    )


def verify_numeric(
    claimed: float, expected: float, *, relative_tolerance: float = 0.01
) -> VerificationResult:
    """Compare with relative tolerance, so 9.81 and 9.8 agree."""
    scale = max(abs(expected), 1e-9)
    error = abs(claimed - expected) / scale
    claim = f"{claimed} ≈ {expected}"
    if error <= relative_tolerance:
        return VerificationResult(
            verdict=VerificationVerdict.VERIFIED,
            method=VerificationMethod.NUMERIC_TOLERANCE,
            detail=f"relative error {error:.3%} within {relative_tolerance:.1%}",
            checked_claim=claim,
        )
    return VerificationResult(
        verdict=VerificationVerdict.REFUTED,
        method=VerificationMethod.NUMERIC_TOLERANCE,
        detail=f"relative error {error:.3%} exceeds {relative_tolerance:.1%}",
        checked_claim=claim,
    )


_UNIT_REGISTRY: Any = None


def _units() -> Any:
    """Pint's registry is expensive to build, so it is created once and reused."""
    global _UNIT_REGISTRY
    if _UNIT_REGISTRY is None:
        import pint

        _UNIT_REGISTRY = pint.UnitRegistry()
    return _UNIT_REGISTRY


# Decimal points and plus signs are required: "2.0 m" and "1.5e+3 J" are ordinary
# answers, and rejecting them would mark a correct student answer unverifiable.
# Quotes, brackets and underscores stay excluded, so this remains a closed set.
_UNIT_PATTERN = re.compile(r"^[A-Za-z°%/·*^0-9.+\s\-]{1,48}$")


def verify_dimensions(quantity: str, expected_dimension: str) -> VerificationResult:
    """Check a quantity carries the dimension the question asked for.

    Catches the classic physics error of producing a number with the wrong
    dimensionality - an answer in metres where seconds were asked for.
    """
    if not _UNIT_PATTERN.match(quantity) or not _UNIT_PATTERN.match(expected_dimension):
        return VerificationResult(
            verdict=VerificationVerdict.NOT_APPLICABLE,
            method=VerificationMethod.DIMENSIONAL,
            detail="unit string outside the accepted character set",
        )
    try:
        registry = _units()
        actual = registry.Quantity(quantity)
        expected = registry.Quantity(1, expected_dimension)
    except Exception as exc:  # noqa: BLE001 - pint raises a wide family
        return VerificationResult(
            verdict=VerificationVerdict.NOT_APPLICABLE,
            method=VerificationMethod.DIMENSIONAL,
            detail=f"unrecognised unit ({type(exc).__name__})",
        )

    claim = f"{quantity} has dimensions of {expected_dimension}"
    if actual.dimensionality == expected.dimensionality:
        return VerificationResult(
            verdict=VerificationVerdict.VERIFIED,
            method=VerificationMethod.DIMENSIONAL,
            detail=f"both are {actual.dimensionality}",
            checked_claim=claim,
        )
    return VerificationResult(
        verdict=VerificationVerdict.REFUTED,
        method=VerificationMethod.DIMENSIONAL,
        detail=f"{actual.dimensionality} is not {expected.dimensionality}",
        checked_claim=claim,
    )


def compare_quantities(
    left: str, right: str, *, relative_tolerance: float = 0.01
) -> VerificationResult:
    """Compare two quantities across units: 2.0 m and 200 cm are equal.

    This is what makes numeric grading fair - a student who answers in
    centimetres has not answered incorrectly.
    """
    for candidate in (left, right):
        if not _UNIT_PATTERN.match(candidate):
            return VerificationResult(
                verdict=VerificationVerdict.NOT_APPLICABLE,
                method=VerificationMethod.DIMENSIONAL,
                detail="unit string outside the accepted character set",
            )
    try:
        registry = _units()
        a = registry.Quantity(left)
        b = registry.Quantity(right)
        if a.dimensionality != b.dimensionality:
            return VerificationResult(
                verdict=VerificationVerdict.REFUTED,
                method=VerificationMethod.DIMENSIONAL,
                detail=f"{a.dimensionality} is not {b.dimensionality}",
                checked_claim=f"{left} == {right}",
            )
        difference = abs((a - b).to(a.units).magnitude)
        scale = max(abs(a.magnitude), 1e-9)
    except Exception as exc:  # noqa: BLE001
        return VerificationResult(
            verdict=VerificationVerdict.NOT_APPLICABLE,
            method=VerificationMethod.DIMENSIONAL,
            detail=f"could not compare ({type(exc).__name__})",
        )

    claim = f"{left} == {right}"
    if difference / scale <= relative_tolerance:
        return VerificationResult(
            verdict=VerificationVerdict.VERIFIED,
            method=VerificationMethod.DIMENSIONAL,
            detail="equal within tolerance after unit conversion",
            checked_claim=claim,
        )
    return VerificationResult(
        verdict=VerificationVerdict.REFUTED,
        method=VerificationMethod.DIMENSIONAL,
        detail=f"differ by {difference:.4g} {a.units:~}",
        checked_claim=claim,
    )


# --- the orchestrating check --------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifiableProblem:
    """What the caller knows about the problem, if anything.

    All fields optional: most tutoring turns supply none of them, and the honest
    result there is NOT_APPLICABLE.
    """

    equation: str | None = None
    variable: str | None = None
    expected_value: float | None = None
    expected_unit: str | None = None
    expected_expression: str | None = None


def verify_answer(answer_text: str, problem: VerifiableProblem) -> VerificationResult:
    """Best available deterministic check on a model's answer.

    Strongest first: substitution beats numeric comparison, which beats a
    dimensional check. If none applies, say so plainly.
    """
    claims = extract_numeric_claims(answer_text)

    if problem.equation and problem.variable:
        for claim in claims:
            if claim.variable == problem.variable or claim.variable is None:
                result = verify_equation_solution(problem.equation, problem.variable, claim.value)
                if result.is_decisive:
                    return result

    if problem.expected_value is not None and claims:
        return verify_numeric(claims[0].value, problem.expected_value)

    if problem.expected_expression:
        return verify_symbolic_equivalence(answer_text.strip(), problem.expected_expression)

    if problem.expected_unit:
        for claim in claims:
            if claim.unit:
                return verify_dimensions(f"{claim.value} {claim.unit}", problem.expected_unit)

    return VerificationResult(
        verdict=VerificationVerdict.NOT_APPLICABLE,
        detail="no machine-checkable claim was available",
    )
