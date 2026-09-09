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

import ast
import operator
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

MAX_FACTORIAL = 1000
"""1000! is 2568 digits and computes in about a millisecond. `factorial` is in
the whitelist for permutations and combinations, and school-level ones are far
below this - but SymPy evaluates the factorial while PARSING, so an unbounded
argument is a denial of service that never reaches the solver at all. Measured:
`factorial(999999)` spends ten seconds of CPU before returning."""

_EXPONENT_OP = re.compile(r"\*\*|\^")
_CHAINED_EXPONENT = re.compile(r"(?:\*\*|\^)\s*[\w.()]*?\s*(?:\*\*|\^)")

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

    # Every exponent that is a NUMBER, however it is spelled.
    #
    # This used to read a bare literal off the raw string, so it saw the 500 in
    # `2**(500*500)` and let a 250000-power through: measured, that expression
    # parses in 8.2 seconds and peaks at 465 MB, because SymPy computes it while
    # parsing. Writing the exponent as a product was the entire bypass.
    #
    # An exponent containing a symbol needs no bound - `x**(a*b)` stays a
    # symbolic Pow and computes nothing - so only fully numeric ones are folded,
    # and the folding itself refuses to exponentiate.
    for fragment in _exponent_operands(text):
        value = _fold_number(fragment)
        if value is not None and abs(value) > MAX_EXPONENT:
            raise UnsafeExpression("exponent_too_large")

    # Same argument for factorial, which also evaluates during parsing. Here an
    # unfoldable argument is rejected rather than allowed: `factorial(x)` is not
    # school-level notation, and permitting it would reopen the hole for
    # anything the folder cannot read.
    for fragment in _call_arguments(text, "factorial"):
        value = _fold_number(fragment)
        if value is None or abs(value) > MAX_FACTORIAL:
            raise UnsafeExpression("factorial_argument_unbounded")


def _balanced(text: str, start: int) -> tuple[str, int] | None:
    """The parenthesised group beginning at `start`, and the index just after it."""
    if start >= len(text) or text[start] != "(":
        return None
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
    return None  # Unbalanced. The parser rejects it a moment later.


def _exponent_operands(text: str) -> list[str]:
    """The right-hand side of every `**` and `^`, as written.

    Textual rather than parsed, because the whole point is to decide BEFORE
    parsing: SymPy does the arithmetic during `parse_expr`, so a check that
    waits for a tree has already paid the cost it exists to prevent.
    """
    operands: list[str] = []
    for match in _EXPONENT_OP.finditer(text):
        index = match.end()
        while index < len(text) and text[index].isspace():
            index += 1
        sign = ""
        while index < len(text) and text[index] in "+-":
            sign += text[index]
            index += 1
            while index < len(text) and text[index].isspace():
                index += 1
        group = _balanced(text, index)
        if group is not None:
            operands.append(sign + "(" + group[0] + ")")
            continue
        end = index
        while end < len(text) and (text[end].isalnum() or text[end] in "._"):
            end += 1
        if end > index:
            operands.append(sign + text[index:end])
    return operands


def _call_arguments(text: str, name: str) -> list[str]:
    """Every argument written to `name(...)`, as text."""
    arguments: list[str] = []
    for match in re.finditer(rf"\b{re.escape(name)}\s*", text):
        group = _balanced(text, match.end())
        if group is not None:
            arguments.append(group[0])
    return arguments


_FOLD_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def _fold_number(fragment: str) -> float | None:
    """The value of a fragment that is pure arithmetic on numbers, else None.

    None means "contains a symbol", which needs no bound: a `Pow` with a free
    symbol in its exponent stays symbolic and computes nothing.

    Deliberately refuses to fold `**`. Folding an exponent would reproduce the
    bomb inside the check meant to catch it, and chained exponents are rejected
    outright a few lines above.
    """
    try:
        tree = ast.parse(fragment.strip(), mode="eval")
    except SyntaxError:
        return None

    def walk(node: ast.expr) -> float | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd | ast.USub):
            inner = walk(node.operand)
            if inner is None:
                return None
            return inner if isinstance(node.op, ast.UAdd) else -inner
        if isinstance(node, ast.BinOp):
            handler = _FOLD_OPS.get(type(node.op))
            if handler is None:
                return None
            left, right = walk(node.left), walk(node.right)
            if left is None or right is None:
                return None
            # Stop the moment either side passes the ceiling rather than
            # carrying the value on: the caller only asks whether the bound was
            # exceeded, and a runaway intermediate is the thing being prevented.
            if abs(left) > MAX_EXPONENT or abs(right) > MAX_EXPONENT:
                return float(MAX_EXPONENT) + 1.0
            try:
                return float(handler(left, right))
            except (ZeroDivisionError, OverflowError, ValueError):
                return None
        return None

    return walk(tree.body)


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
