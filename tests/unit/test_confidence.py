"""Confidence scoring and verifier selectivity.

The contract under test: the band comes from observable signals, never from the
model's own claim about itself.
"""

from __future__ import annotations

from tutortwin.capabilities.executor import (
    HEDGE_DENSITY_THRESHOLD,
    score_confidence,
    should_verify,
)
from tutortwin.domain.budget import (
    BudgetOutcome,
    BudgetReason,
    ExecutionBudgetDecision,
    VerifierMode,
)
from tutortwin.domain.capabilities import CapabilityId, ConfidenceBand
from tutortwin.domain.provider import ModelAlias, ModelCall, Provider, StopReason


def call(text: str, stop: StopReason = StopReason.END_TURN) -> ModelCall:
    return ModelCall(
        alias=ModelAlias.STANDARD_TUTOR,
        provider=Provider.FAKE,
        model_id="fake",
        text=text,
        stop_reason=stop,
    )


GOOD = (
    "Photosynthesis converts light energy into chemical energy. Chlorophyll in "
    "the chloroplast absorbs light, which splits water and drives the production "
    "of glucose from carbon dioxide."
)


def test_clean_non_stem_answer_is_high() -> None:
    band, signals = score_confidence(call(GOOD), CapabilityId.BIOLOGY)
    assert band is ConfidenceBand.HIGH
    assert signals == ()


def test_clean_stem_answer_is_capped_at_medium() -> None:
    """Unverified arithmetic is a known weak spot, so STEM never claims HIGH."""
    band, signals = score_confidence(call(GOOD), CapabilityId.MATH)
    assert band is ConfidenceBand.MEDIUM
    assert "stem_unverified" in signals


def test_refusal_is_low() -> None:
    band, signals = score_confidence(call("", StopReason.REFUSAL), CapabilityId.BIOLOGY)
    assert band is ConfidenceBand.LOW
    assert signals == ("refusal",)


def test_empty_response_is_low() -> None:
    band, signals = score_confidence(call("   "), CapabilityId.BIOLOGY)
    assert band is ConfidenceBand.LOW
    assert "empty_response" in signals


def test_truncation_is_low_regardless_of_quality() -> None:
    """A cut-off answer is missing its conclusion, however good the prefix reads."""
    band, signals = score_confidence(call(GOOD, StopReason.MAX_TOKENS), CapabilityId.BIOLOGY)
    assert band is ConfidenceBand.LOW
    assert "truncated_output" in signals


def test_self_contradiction_is_low() -> None:
    text = f"{GOOD} Actually, wait - that is wrong, let me redo it."
    band, signals = score_confidence(call(text), CapabilityId.BIOLOGY)
    assert band is ConfidenceBand.LOW
    assert "self_contradiction" in signals


def test_stated_inability_is_low() -> None:
    band, signals = score_confidence(
        call("I don't know enough about this to answer properly."), CapabilityId.BIOLOGY
    )
    assert band is ConfidenceBand.LOW
    assert "stated_inability" in signals


def test_occasional_hedging_is_tolerated() -> None:
    """Teaching language hedges normally - density is the signal, not presence."""
    text = f"This probably helps: {GOOD}"
    band, _ = score_confidence(call(text), CapabilityId.BIOLOGY)
    assert band is ConfidenceBand.HIGH


def test_dense_hedging_lowers_confidence() -> None:
    text = (
        "I think it might be photosynthesis, though I'm not sure. It seems the "
        "chloroplast is possibly involved, and perhaps the answer is roughly this."
    )
    band, signals = score_confidence(call(text), CapabilityId.BIOLOGY)
    assert band is ConfidenceBand.MEDIUM
    assert any(s.startswith("hedging_x") for s in signals)
    hedges = int(next(s for s in signals if s.startswith("hedging_x")).split("x")[1])
    assert hedges >= HEDGE_DENSITY_THRESHOLD


def test_very_short_answer_is_flagged() -> None:
    band, signals = score_confidence(call("Yes."), CapabilityId.BIOLOGY)
    assert "very_short_answer" in signals
    assert band is not ConfidenceBand.HIGH


def test_scoring_is_deterministic() -> None:
    assert score_confidence(call(GOOD), CapabilityId.MATH) == score_confidence(
        call(GOOD), CapabilityId.MATH
    )


# --- Verifier selectivity -----------------------------------------------------


def decision(mode: VerifierMode) -> ExecutionBudgetDecision:
    return ExecutionBudgetDecision(
        outcome=BudgetOutcome.ALLOW_WITH_VERIFICATION,
        reason=BudgetReason.ADVANCED_STEM,
        alias=ModelAlias.ADVANCED_REASONING,
        verifier_mode=mode,
        verifier_alias=ModelAlias.VERIFIER_PRIMARY if mode is not VerifierMode.NONE else None,
    )


def test_verifier_never_runs_when_disarmed() -> None:
    assert (
        should_verify(
            decision=decision(VerifierMode.NONE),
            confidence=ConfidenceBand.LOW,
            capability=CapabilityId.MATH,
        )
        is False
    )


def test_confident_answer_does_not_trigger_a_second_call() -> None:
    """The core cost claim: a good answer costs exactly one call."""
    for band in (ConfidenceBand.HIGH, ConfidenceBand.MEDIUM):
        assert (
            should_verify(
                decision=decision(VerifierMode.ON_LOW_CONFIDENCE),
                confidence=band,
                capability=CapabilityId.MATH,
            )
            is False
        )


def test_low_confidence_stem_triggers_verifier() -> None:
    assert (
        should_verify(
            decision=decision(VerifierMode.ON_LOW_CONFIDENCE),
            confidence=ConfidenceBand.LOW,
            capability=CapabilityId.MATH,
        )
        is True
    )


def test_low_confidence_non_stem_does_not_trigger_verifier() -> None:
    assert (
        should_verify(
            decision=decision(VerifierMode.ON_LOW_CONFIDENCE),
            confidence=ConfidenceBand.LOW,
            capability=CapabilityId.WRITING_FEEDBACK,
        )
        is False
    )


def test_always_mode_verifies_even_when_confident() -> None:
    assert (
        should_verify(
            decision=decision(VerifierMode.ALWAYS),
            confidence=ConfidenceBand.HIGH,
            capability=CapabilityId.MATH,
        )
        is True
    )
