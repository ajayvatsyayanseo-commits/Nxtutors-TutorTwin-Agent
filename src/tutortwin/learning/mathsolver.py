"""Compute the answer, then let the model explain it.

The shape is: **student question -> SymPy -> exact result -> model explains**.
The model never computes; it narrates a result that is already correct.

This is the difference between a tutor that is usually right and one that is
provably right on the part that can be checked. A language model asked to
integrate `3x^3 + 3x^2` will usually produce `3x^4/4 + x^3 + C`, and will
occasionally produce something subtly wrong with total confidence - which is the
worst possible failure for a student who cannot yet tell the difference. SymPy
either returns the right answer or raises.

**Nothing here trusts its input.** Parsing reuses `verification.safe_parse`,
which is already hardened against `sympify` escapes, exponent bombs and
attribute access. There is deliberately no second parser in this codebase.

Scope note: SymPy, not Sage. Sage is a ~2GB distribution that bundles SymPy
among fifty other systems; everything school and early-undergraduate
mathematics needs is in SymPy alone, and the install cost would land on every
container image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

import sympy
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

from tutortwin.learning.verification import _SAFE_NAMES, UnsafeExpression, assert_parseable
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

# Students write `2x`, `3x^2` and `(x+2)(x+3)`. SymPy's standard transformations
# reject all three, which is correct for `verification.safe_parse` - that reads
# *model* output, which writes `2*x` - and wrong here, where the input is what a
# fifteen-year-old typed into WhatsApp.
#
# This adds only a PARSING transformation. The security layer is unchanged and
# unchanged deliberately: `assert_parseable` still runs first, on the raw string,
# and `_SAFE_NAMES` is still the only namespace, so no name outside the maths
# whitelist can resolve. Implicit multiplication cannot reach an attribute or a
# builtin; it only inserts `*` between adjacent factors.
_SOLVER_TRANSFORMS = (
    *standard_transformations,
    convert_xor,
    implicit_multiplication_application,
)


def _parse(text: str) -> sympy.Expr:
    """Parse student-written maths. Same guard as `safe_parse`, looser grammar."""
    cleaned = text.strip()
    assert_parseable(cleaned)
    try:
        expression = parse_expr(
            cleaned,
            transformations=_SOLVER_TRANSFORMS,
            global_dict=dict(_SAFE_NAMES),
            evaluate=True,
        )
    except Exception as exc:  # noqa: BLE001 - any parse failure is a rejection
        raise UnsafeExpression(f"parse_failed:{type(exc).__name__}") from exc
    if not isinstance(expression, sympy.Basic):
        raise UnsafeExpression("not_an_expression")
    return expression


TIMEOUT_SECONDS = 5.0
"""SymPy can take unbounded time on a pathological integral. The solve runs in a
worker thread and the caller stops waiting; a slow problem degrades to the
model's own answer rather than pinning a request."""


class Operation(StrEnum):
    SOLVE = "SOLVE"
    DIFFERENTIATE = "DIFFERENTIATE"
    INTEGRATE = "INTEGRATE"
    SIMPLIFY = "SIMPLIFY"
    FACTOR = "FACTOR"
    EXPAND = "EXPAND"
    LIMIT = "LIMIT"
    EVALUATE = "EVALUATE"


@dataclass(frozen=True, slots=True)
class MathSolution:
    """An exact result, with the pieces a tutor needs to explain it."""

    operation: Operation
    problem: str
    """The expression as parsed, echoed back so a misparse is visible."""

    result: str
    """Plain-text form, for the message body."""

    latex: str
    """Typeset form, for the image the student actually reads."""

    steps: tuple[str, ...] = ()
    """Intermediate results where SymPy can supply them honestly. Empty is the
    normal case - inventing plausible-looking steps would be worse than none."""

    variable: str | None = None
    verified: bool = False
    """True only when the result was substituted back and checked. A solver that
    claims verification it did not perform is worse than one that stays quiet."""


class Unsolvable(ValueError):
    """Not a problem this can compute. The model answers instead."""


# What the student is asking for, in the words students actually use.
_INTENT: tuple[tuple[Operation, re.Pattern[str]], ...] = (
    (Operation.DIFFERENTIATE, re.compile(r"\b(differentiate|derivative|d/d[a-z]|dy/dx)\b", re.I)),
    (Operation.INTEGRATE, re.compile(r"\b(integrate|integral|antiderivative)\b|∫", re.I)),
    (Operation.LIMIT, re.compile(r"\blimit\b|\blim\b", re.I)),
    (Operation.FACTOR, re.compile(r"\bfactoris|\bfactoriz|\bfactor\b", re.I)),
    (Operation.EXPAND, re.compile(r"\bexpand\b", re.I)),
    (Operation.SIMPLIFY, re.compile(r"\bsimplif", re.I)),
    (Operation.SOLVE, re.compile(r"\bsolve\b|\bfind\s+(?:the\s+)?value|\broots?\b", re.I)),
    (Operation.EVALUATE, re.compile(r"\b(evaluate|calculate|compute|work out)\b", re.I)),
)

# `\int`, `\frac{d}{dx}` and friends, so a LaTeX-formatted question routes too.
_LATEX_INTENT: tuple[tuple[Operation, re.Pattern[str]], ...] = (
    (Operation.INTEGRATE, re.compile(r"\\int\b")),
    (Operation.DIFFERENTIATE, re.compile(r"\\frac\s*\{\s*d\s*\}\s*\{\s*d[a-z]\s*\}")),
    (Operation.LIMIT, re.compile(r"\\lim\b")),
)

_DX = re.compile(r"\bd([a-z])\b|\\,?\s*d([a-z])\b")
_WRT = re.compile(r"with respect to\s+([a-z])\b", re.I)
_SOLVE_FOR = re.compile(r"\bfor\s+([a-z])\b", re.I)


# Every multi-letter name that may legitimately survive into an expression.
# Derived from the parser's own whitelist so the two cannot drift: a function
# the parser accepts but this rejects would refuse valid maths, and the reverse
# would let an English word through as a product of symbols.
_KNOWN_NAMES: frozenset[str] = frozenset(
    {name.lower() for name in _SAFE_NAMES}
    | {"pi", "oo", "inf", "infinity", "ln", "log", "exp", "abs", "sqrt"}
)


def detect_operation(question: str) -> Operation | None:
    """What is being asked. None means "not a computation" - answer in prose."""
    for operation, pattern in _LATEX_INTENT:
        if pattern.search(question):
            return operation
    for operation, pattern in _INTENT:
        if pattern.search(question):
            return operation
    # A bare equation with an `=` and a symbol is a solve request even unasked.
    if "=" in question and re.search(r"[a-zA-Z]", question):
        return Operation.SOLVE
    return None


def _strip_latex(text: str) -> str:
    """Turn the LaTeX a model or student writes into something SymPy parses.

    Only the constructs that actually appear in school maths. Anything left
    unconverted fails the safe parse, which is the correct outcome - a partial
    conversion that silently changes the meaning of the expression would be far
    worse than a refusal.
    """
    out = text
    # The derivative operator must be removed BEFORE the generic \frac rule, or
    # `\frac{d}{dx}(x^3+2x)` becomes the fraction ((d)/(dx)) multiplied by the
    # expression - which parses, evaluates, and returns nonsense with total
    # confidence. The operator carries no value; `_LATEX_INTENT` already
    # captured the intent from it.
    out = re.sub(r"\\frac\s*\{\s*d\s*\}\s*\{\s*d\s*([a-z])\s*\}", " ", out)
    out = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"((\1)/(\2))", out)
    out = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", out)
    out = re.sub(r"\\(left|right)", "", out)
    out = re.sub(r"\\(cdot|times)", "*", out)
    out = re.sub(r"\\div", "/", out)
    out = re.sub(r"\\pi\b", "pi", out)
    out = re.sub(r"\\(sin|cos|tan|log|ln|exp)\b", r"\1", out)
    out = re.sub(r"\\int\b|\\lim\b|\\,|\\;|\\!|\$", " ", out)
    out = re.sub(r"\bd[a-z]\b\s*$", "", out.strip())
    out = out.replace("{", "(").replace("}", ")")
    return out.strip()


def extract_expression(question: str) -> str:
    """Pull the mathematics out of a sentence around it."""
    text = _strip_latex(question)

    # The variable clause goes FIRST, and takes the variable name with it.
    #
    # Removing only the words "with respect to" from
    # `differentiate x^3 + 2x with respect to x` leaves `x^3 + 2x   x`, and
    # implicit multiplication then reads the dangling `x` as another factor -
    # silently turning `2x` into `2x^2` and returning 3x^2+4x for a derivative
    # that is 3x^2+2. A confidently wrong answer is the single worst thing this
    # module can produce, so the whole clause is removed as a unit.
    text = re.sub(r"\bwith\s+respect\s+to\s+[a-z]\b", " ", text, flags=re.I)
    text = re.sub(r"\bfor\s+[a-z]\s*$", " ", text.strip(), flags=re.I)
    text = re.sub(r"\bin\s+terms\s+of\s+[a-z]\b", " ", text, flags=re.I)

    # Drop the remaining instruction words; what is left should be the expression.
    # Note `for` and `with respect to` are NOT in this list - they are handled
    # above, with their variable.
    text = re.sub(
        r"\b(please|kindly|can you|could you|help me|solve|integrate|differentiate|"
        r"simplify|expand|factorise|factorize|factor|evaluate|calculate|compute|"
        r"work out|find|the|value|of|derivative|integral|antiderivative|limit|"
        r"answer|question|this|equation)\b",
        " ",
        text,
        flags=re.I,
    )
    # The differential goes last: "integrate (3x^3+3x^2) dx" only ends in `dx`
    # once the word "integrate" has already been removed.
    text = re.sub(r"d\s*[a-z]\s*$", "", text.strip())
    text = re.sub(r"^[\s:,.?-]+|[\s:,.?]+$", "", text)
    text = re.sub(r"\s{2,}", " ", text)

    # Refuse anything still carrying an English word.
    #
    # This is the most dangerous failure this module can have, and it is silent.
    # The instruction list above is a blocklist, so any word not on it survives
    # into the expression, where implicit multiplication turns it into a product
    # of single-letter symbols: `solve 2x + 5 = 13 quickly` returned
    # `x = 13*c*i*k*l*q*u*y/2 - 5/2`, and the substitution check passed, so it
    # was reported to the student as VERIFIED.
    #
    # A blocklist cannot be completed - students write "urgently", "asap", "sir".
    # So the last word belongs to an allowlist: every remaining multi-letter run
    # must be a mathematical function name, or this is not an expression and the
    # model answers instead.
    for word in re.findall(r"[A-Za-z]{2,}", text):
        if word.lower() not in _KNOWN_NAMES:
            raise Unsolvable(f"unrecognised word in expression: {word!r}")

    if not text:
        raise Unsolvable("no expression found")
    return text


def _variable(question: str, expression: sympy.Basic, operation: Operation) -> sympy.Symbol:
    """Which variable the operation is about."""
    named = _WRT.search(question) or (
        _SOLVE_FOR.search(question) if operation is Operation.SOLVE else None
    )
    if named:
        return sympy.Symbol(named.group(1))

    if operation in (Operation.INTEGRATE, Operation.DIFFERENTIATE):
        marker = _DX.search(question)
        if marker:
            return sympy.Symbol(marker.group(1) or marker.group(2))

    free = sorted(expression.free_symbols, key=lambda s: str(s))
    if not free:
        raise Unsolvable("no variable to operate on")
    # `x` if present - it is what a student almost always means.
    for symbol in free:
        if str(symbol) == "x":
            return symbol
    return free[0]


def _to_equation(text: str) -> sympy.Basic:
    """`2x + 5 = 13` becomes `Eq(2x+5, 13)`; a bare expression stays itself."""
    if text.count("=") == 1:
        left, right = text.split("=")
        return sympy.Eq(_parse(left), _parse(right))
    return _parse(text)


def _format(value: object) -> str:
    return str(value)


def _solve_sync(question: str, operation: Operation) -> MathSolution:
    """The actual computation. Runs in a worker thread."""
    raw = extract_expression(question)
    parsed = _to_equation(raw)
    # lhs - rhs, NOT rhs - lhs. Backwards, `factorise x^2 - 9 = 0` returns
    # -(x-3)(x+3): a valid factorisation of the negation, and the wrong answer
    # to the question asked - reported as VERIFIED, because substituting into a
    # negated expression still gives zero.
    expression = parsed.lhs - parsed.rhs if isinstance(parsed, sympy.Eq) else parsed
    variable = _variable(question, parsed, operation)
    steps: list[str] = []
    verified = False

    if operation is Operation.SOLVE:
        roots = sympy.solve(parsed, variable, dict=False)
        if not roots:
            raise Unsolvable("no solution found")
        # Substitute each root back. This is the whole reason to compute
        # symbolically rather than ask a model: the answer can be *checked*.
        verified = all(
            sympy.simplify(
                (parsed.lhs - parsed.rhs) if isinstance(parsed, sympy.Eq) else parsed
            ).subs(variable, root)
            == 0
            for root in roots
        )
        result = ", ".join(f"{variable} = {_format(r)}" for r in roots)
        latex = r",\quad ".join(f"{sympy.latex(variable)} = {sympy.latex(r)}" for r in roots)

    elif operation is Operation.DIFFERENTIATE:
        answer = sympy.diff(expression, variable)
        result = _format(answer)
        latex = (
            rf"\frac{{d}}{{d{sympy.latex(variable)}}}"
            rf"\left({sympy.latex(expression)}\right) = {sympy.latex(answer)}"
        )

    elif operation is Operation.INTEGRATE:
        answer = sympy.integrate(expression, variable)
        if answer.has(sympy.Integral):
            raise Unsolvable("integral has no closed form")
        result = f"{_format(answer)} + C"
        latex = (
            rf"\int {sympy.latex(expression)}\,d{sympy.latex(variable)} = "
            rf"{sympy.latex(answer)} + C"
        )
        # Differentiating the result must return the integrand. Cheap, and it
        # catches the rare case where SymPy returns something unsimplified.
        verified = sympy.simplify(sympy.diff(answer, variable) - expression) == 0

    elif operation is Operation.SIMPLIFY:
        answer = sympy.simplify(expression)
        result = _format(answer)
        latex = rf"{sympy.latex(expression)} = {sympy.latex(answer)}"
        verified = sympy.simplify(answer - expression) == 0

    elif operation is Operation.FACTOR:
        answer = sympy.factor(expression)
        result = _format(answer)
        latex = rf"{sympy.latex(expression)} = {sympy.latex(answer)}"
        verified = sympy.expand(answer - expression) == 0

    elif operation is Operation.EXPAND:
        answer = sympy.expand(expression)
        result = _format(answer)
        latex = rf"{sympy.latex(expression)} = {sympy.latex(answer)}"
        verified = sympy.simplify(answer - expression) == 0

    elif operation is Operation.EVALUATE:
        answer = sympy.simplify(expression)
        numeric = answer.evalf() if not answer.free_symbols else None
        result = _format(answer) if numeric is None else f"{answer} = {numeric}"
        latex = rf"{sympy.latex(expression)} = {sympy.latex(answer)}"

    else:  # LIMIT
        raise Unsolvable("limits need a point, which is not parsed yet")

    return MathSolution(
        operation=operation,
        problem=_format(parsed),
        result=result,
        latex=latex,
        steps=tuple(steps),
        variable=str(variable),
        verified=verified,
    )


async def solve(question: str) -> MathSolution | None:
    """Compute an exact answer, or None if this is not a computation.

    None is the normal, common outcome - most tutoring questions are prose. It
    is never an error, and the caller falls through to the model.
    """
    operation = detect_operation(question)
    if operation is None:
        return None

    import asyncio

    try:
        solution = await asyncio.wait_for(
            asyncio.to_thread(_solve_sync, question, operation), timeout=TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.info("math_solve_timeout", operation=operation.value)
        return None
    except (Unsolvable, UnsafeExpression) as exc:
        logger.info("math_not_solvable", operation=operation.value, reason=str(exc))
        return None
    except (ValueError, TypeError, AttributeError, NotImplementedError, RecursionError) as exc:
        # SymPy raises a wide and undocumented range on input it dislikes. A
        # solver failure must never fail the student's request.
        logger.info("math_solve_failed", operation=operation.value, error=type(exc).__name__)
        return None

    logger.info(
        "math_solved",
        operation=solution.operation.value,
        verified=solution.verified,
        variable=solution.variable,
    )
    return solution


def as_prompt_context(solution: MathSolution) -> str:
    """What the model is told, so it explains rather than recomputes.

    Phrased as a fact the tutor already knows, with an explicit instruction not
    to contradict it: a model handed a correct answer will still occasionally
    "correct" it to a wrong one if the prompt reads like a suggestion.
    """
    lines = [
        "A symbolic mathematics engine has already computed this exactly.",
        f"Operation: {solution.operation.value}",
        f"Problem as parsed: {solution.problem}",
        f"Exact result: {solution.result}",
    ]
    if solution.verified:
        lines.append("This result was checked by substitution and is correct.")
    lines.append(
        "Explain how to reach this result, step by step, in your own teaching voice. "
        "Do NOT recompute it and do NOT contradict it. If the parsed problem does not "
        "match what the student asked, say so instead of answering."
    )
    return "\n".join(lines)


__all__ = [
    "TIMEOUT_SECONDS",
    "MathSolution",
    "Operation",
    "Unsolvable",
    "as_prompt_context",
    "detect_operation",
    "extract_expression",
    "solve",
]
