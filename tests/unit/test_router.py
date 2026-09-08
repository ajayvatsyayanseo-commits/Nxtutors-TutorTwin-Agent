"""Deterministic routing. Every assertion here must hold with zero model calls."""

from __future__ import annotations

import pytest

from tutortwin.domain.capabilities import CapabilityId, Difficulty, PedagogyMode
from tutortwin.orchestration.router import (
    classify,
    detect_follow_up,
    detect_pedagogy_request,
    estimate_difficulty,
    score_capabilities,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Explain photosynthesis to me", CapabilityId.BIOLOGY),
        ("What is mitosis?", CapabilityId.BIOLOGY),
        ("Solve for x: 2x + 5 = 13", CapabilityId.MATH),
        ("Integrate x^2 dx", CapabilityId.MATH),
        ("Prove this Taylor series converges", CapabilityId.MATH),
        ("A ball is thrown with velocity 20 m/s, find the acceleration", CapabilityId.PHYSICS),
        ("What is Newton's second law?", CapabilityId.PHYSICS),
        ("Balance the equation for this reaction", CapabilityId.CHEMISTRY),
        ("Explain stoichiometry", CapabilityId.CHEMISTRY),
        ("My python code throws a traceback, please debug", CapabilityId.CODING),
        ("Check my answer: I got 42", CapabilityId.ANSWER_CHECK),
        ("Give me feedback on my essay introduction", CapabilityId.WRITING_FEEDBACK),
        ("What is the past tense grammar rule?", CapabilityId.LANGUAGE_HELP),
        ("Translate this into french", CapabilityId.LANGUAGE_HELP),
    ],
)
def test_routes_without_a_model_call(text: str, expected: CapabilityId) -> None:
    decision = classify(text)
    assert decision.capability is expected
    # The cost claim: routing never pays a provider.
    assert decision.used_model is False
    assert decision.reason


def test_empty_message_is_unknown() -> None:
    decision = classify("")
    assert decision.capability is CapabilityId.UNKNOWN
    assert decision.used_model is False


def test_unmatched_text_falls_back_without_a_model_call() -> None:
    """A greeting is not ambiguous, it is trivial - it must not trigger a call."""
    decision = classify("hey")
    assert decision.capability is CapabilityId.GENERAL_TUTORING
    assert decision.used_model is False


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("explain simpler please", PedagogyMode.GUIDED),
        ("just give me the answer", PedagogyMode.ANSWER_AND_EXPLAIN),
        ("show me step by step", PedagogyMode.STEP_BY_STEP),
        ("give me a hint", PedagogyMode.HINT_FIRST),
        ("ask me questions instead", PedagogyMode.SOCRATIC),
        ("I need exam revision", PedagogyMode.EXAM_REVISION),
    ],
)
def test_explicit_pedagogy_requests_are_detected(text: str, mode: PedagogyMode) -> None:
    assert detect_pedagogy_request(text) is mode
    assert classify(text).requested_mode is mode


def test_no_pedagogy_request_returns_none() -> None:
    assert detect_pedagogy_request("what is osmosis") is None


@pytest.mark.parametrize(
    "text", ["why step 2?", "why did you do that", "explain that again", "and then?"]
)
def test_follow_up_detected_only_with_history(text: str) -> None:
    assert detect_follow_up(text, has_history=True) is True
    # Without prior turns there is nothing to follow up on.
    assert detect_follow_up(text, has_history=False) is False


def test_follow_up_inherits_conversation_topic() -> None:
    decision = classify("why step 2?", has_history=True)
    assert decision.is_follow_up is True
    assert decision.capability is CapabilityId.GENERAL_TUTORING
    assert "follow_up" in decision.reason


def test_long_message_is_not_treated_as_follow_up() -> None:
    text = "Please explain how the citric acid cycle produces ATP in eukaryotic cells"
    assert detect_follow_up(text, has_history=True) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What is a cell?", Difficulty.SIMPLE),
        ("Prove this from first principles", Difficulty.ADVANCED),
        ("Solve this partial differential equation", Difficulty.ADVANCED),
        ("Work out the momentum of the trolley after impact", Difficulty.MODERATE),
    ],
)
def test_difficulty_is_deterministic(text: str, expected: Difficulty) -> None:
    capability = classify(text).capability
    assert estimate_difficulty(text, capability) is expected
    # Same input, same answer - no clock, no randomness.
    assert estimate_difficulty(text, capability) is expected


def test_scoring_is_transparent() -> None:
    """The score is the audit trail for 'why this capability'."""
    scores = score_capabilities("explain photosynthesis and the enzyme involved")
    assert scores[CapabilityId.BIOLOGY] >= 4


def test_subject_marker_outranks_generic_opener() -> None:
    """'what is X' fires on nearly every question; the subject must win."""
    scores = score_capabilities("what is photosynthesis")
    assert scores[CapabilityId.BIOLOGY] > scores.get(CapabilityId.EXPLAIN_CONCEPT, 0)


def test_classification_is_stable_across_calls() -> None:
    text = "Solve for x: 3x - 7 = 14"
    first, second = classify(text), classify(text)
    assert first == second
