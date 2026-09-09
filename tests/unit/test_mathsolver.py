"""The solver, checked against arithmetic rather than against itself.

Every case here is a real bug this module shipped. All three produced a WRONG
ANSWER rather than no answer, and two of them were reported to the student as
VERIFIED - which is the worst outcome this codebase can produce, because a
student who could tell it was wrong would not have needed to ask.
"""

from __future__ import annotations

import pytest

from tutortwin.learning import mathsolver as ms


class TestCorrectness:
    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("integrate (3x^3 + 3x^2) dx", "3*x**4/4 + x**3 + C"),
            ("solve 2x + 5 = 13", "x = 4"),
            ("solve x^2 - 5x + 6 = 0", "x = 2, x = 3"),
            ("expand (x+2)(x+3)", "x**2 + 5*x + 6"),
            ("simplify (x^2-1)/(x-1)", "x + 1"),
            ("integrate sin(x) dx", "-cos(x) + C"),
            ("solve 3y - 9 = 0 for y", "y = 3"),
            ("factorise x^2 - 9", "(x - 3)*(x + 3)"),
        ],
    )
    def test_school_maths(self, question: str, expected: str) -> None:
        import asyncio

        solution = asyncio.run(ms.solve(question))
        assert solution is not None
        assert solution.result == expected


class TestTheSignBug:
    """`rhs - lhs` instead of `lhs - rhs`.

    `factorise x^2 - 9 = 0` returned `-(x - 3)*(x + 3)` - a valid factorisation
    of the NEGATION, and the wrong answer to the question asked. It passed the
    substitution check, because substituting into a negated expression still
    gives zero, so it was reported as VERIFIED.
    """

    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("factorise x^2 - 9 = 0", "(x - 3)*(x + 3)"),
            ("simplify x^2 - 1 = 0", "x**2 - 1"),
            ("expand (x+1)(x+1) = 0", "x**2 + 2*x + 1"),
        ],
    )
    def test_an_equation_keeps_its_sign(self, question: str, expected: str) -> None:
        import asyncio

        solution = asyncio.run(ms.solve(question))
        assert solution is not None
        assert not solution.result.startswith("-"), "the sign was inverted"
        assert solution.result == expected


class TestStrayWords:
    """The most dangerous bug this module had, and it was silent.

    The instruction filter is a blocklist, so any word not on it survived into
    the expression, where implicit multiplication made it a product of
    single-letter symbols. `solve 2x + 5 = 13 quickly` returned
    `x = 13*c*i*k*l*q*u*y/2 - 5/2`, marked VERIFIED.

    A blocklist cannot be completed - students write "urgently", "asap", "sir".
    So the last word is an allowlist, and anything else refuses.
    """

    @pytest.mark.parametrize(
        "question",
        [
            "solve 2x + 5 = 13 quickly",
            "solve 2x + 5 = 13 please help",
            "solve this urgently sir 2x = 8",
            "solve 2x = 8 asap thanks",
            "factorise x^2 - 9 ok",
        ],
    )
    def test_an_unrecognised_word_refuses_rather_than_inventing_symbols(
        self, question: str
    ) -> None:
        import asyncio

        # None is correct: the model answers instead, in prose.
        assert asyncio.run(ms.solve(question)) is None

    def test_real_function_names_still_work(self) -> None:
        """The allowlist must not refuse legitimate mathematics."""
        import asyncio

        for question in ("integrate sin(x) dx", "simplify log(x) + log(x)", "solve sqrt(x) = 3"):
            assert asyncio.run(ms.solve(question)) is not None, question


class TestLatex:
    """`\frac{d}{dx}` is an operator, not a fraction.

    Converted by the generic \frac rule it became `((d)/(dx))` multiplied by
    the expression, which parses, evaluates, and returns nonsense:
    `(3x^2+2)/x - (x^3+2x)/x^2` for a derivative that is `3x^2 + 2`.
    """

    def test_the_derivative_operator_is_not_read_as_a_fraction(self) -> None:
        import asyncio

        solution = asyncio.run(ms.solve(r"\frac{d}{dx}(x^3 + 2x)"))
        assert solution is not None
        assert solution.result == "3*x**2 + 2"

    def test_the_integral_form_still_works(self) -> None:
        import asyncio

        solution = asyncio.run(ms.solve(r"\int (3x^3 + 3x^2)\,dx"))
        assert solution is not None
        assert solution.result == "3*x**4/4 + x**3 + C"


class TestRefusal:
    @pytest.mark.parametrize(
        "question",
        [
            "What is photosynthesis?",
            "__import__('os').system('ls')",
            "tell me about the French revolution",
        ],
    )
    def test_non_maths_is_left_to_the_model(self, question: str) -> None:
        import asyncio

        assert asyncio.run(ms.solve(question)) is None

    def test_verified_is_only_set_when_a_check_actually_ran(self) -> None:
        """Claiming verification that did not happen is worse than none."""
        import asyncio

        # Differentiation is exact but not independently re-checked here.
        solution = asyncio.run(ms.solve("differentiate x^3 + 2x with respect to x"))
        assert solution is not None
        assert solution.verified is False

        # A solve IS checked, by substituting the roots back.
        solution = asyncio.run(ms.solve("solve 2x + 5 = 13"))
        assert solution is not None
        assert solution.verified is True
