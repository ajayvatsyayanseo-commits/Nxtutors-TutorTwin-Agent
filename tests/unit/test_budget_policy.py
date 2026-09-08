"""Cost-aware routing. The decisive property: no rejection can ever spend."""

from __future__ import annotations

import pytest

from tutortwin.domain.budget import (
    BudgetOutcome,
    BudgetReason,
    QuotaSnapshot,
    VerifierMode,
)
from tutortwin.domain.capabilities import CapabilityId, Difficulty
from tutortwin.domain.provider import ModelAlias
from tutortwin.policies.budget_policy import (
    FREE_PLAN,
    MAX_CONTEXT_TOKENS,
    PRO_PLAN,
    PROVIDER_FAILURE_CIRCUIT,
    BudgetContext,
    PlanPolicy,
    decide,
)


def ctx(**overrides: object) -> BudgetContext:
    base: dict[str, object] = {
        "plan": PRO_PLAN,
        "capability": CapabilityId.MATH,
        "difficulty": Difficulty.MODERATE,
        "estimated_context_tokens": 500,
        "quota": QuotaSnapshot(),
    }
    base.update(overrides)
    return BudgetContext(**base)  # type: ignore[arg-type]


# --- The cost gate ------------------------------------------------------------


def test_free_plan_never_spends() -> None:
    decision = decide(ctx(plan=FREE_PLAN))
    assert decision.outcome is BudgetOutcome.REJECT_PLAN
    assert decision.reason is BudgetReason.PLAN_DISALLOWS_AI
    assert decision.permits_paid_call is False
    # No alias means a provider call is not merely disallowed, it is impossible.
    assert decision.alias is None


def test_kill_switch_beats_everything() -> None:
    decision = decide(ctx(ai_enabled=False))
    assert decision.outcome is BudgetOutcome.FEATURE_DISABLED
    assert decision.permits_paid_call is False


def test_daily_call_quota_exhausted_blocks_spend() -> None:
    quota = QuotaSnapshot(calls_today=200, daily_call_limit=200)
    decision = decide(ctx(quota=quota))
    assert decision.outcome is BudgetOutcome.REJECT_QUOTA
    assert decision.reason is BudgetReason.DAILY_QUOTA_EXHAUSTED
    assert decision.permits_paid_call is False


def test_user_budget_exhausted_blocks_spend() -> None:
    quota = QuotaSnapshot(spend_today_micros=2_000_000, user_daily_budget_micros=2_000_000)
    decision = decide(ctx(quota=quota))
    assert decision.reason is BudgetReason.USER_BUDGET_EXHAUSTED
    assert decision.permits_paid_call is False


def test_system_budget_beats_user_quota() -> None:
    """A blown system budget must fail closed even for a student with quota left."""
    quota = QuotaSnapshot(
        system_spend_today_micros=999,
        system_daily_budget_micros=100,
        calls_today=0,
        daily_call_limit=200,
    )
    decision = decide(ctx(quota=quota))
    assert decision.outcome is BudgetOutcome.REJECT_SYSTEM_BUDGET
    assert decision.reason is BudgetReason.SYSTEM_BUDGET_EXHAUSTED


def test_provider_circuit_breaker_stops_paying_to_retry() -> None:
    quota = QuotaSnapshot(recent_provider_failures=PROVIDER_FAILURE_CIRCUIT)
    decision = decide(ctx(quota=quota))
    assert decision.reason is BudgetReason.PROVIDER_UNHEALTHY
    assert decision.permits_paid_call is False


def test_oversized_context_is_rejected_not_truncated() -> None:
    """Silent truncation yields a confidently wrong answer; rejection does not."""
    decision = decide(ctx(estimated_context_tokens=MAX_CONTEXT_TOKENS + 1))
    assert decision.outcome is BudgetOutcome.REJECT_SIZE
    assert decision.permits_paid_call is False


def test_unavailable_capability_is_refused() -> None:
    decision = decide(ctx(capability=CapabilityId.IMAGE_QA))
    assert decision.outcome is BudgetOutcome.FEATURE_DISABLED
    assert decision.reason is BudgetReason.CAPABILITY_NOT_AVAILABLE
    assert decision.permits_paid_call is False


def test_capability_outside_plan_matrix_is_refused() -> None:
    plan = PlanPolicy(
        plan_code="LITE",
        allows_paid_ai=True,
        allowed_capabilities=frozenset({CapabilityId.EXPLAIN_CONCEPT}),
    )
    decision = decide(ctx(plan=plan, capability=CapabilityId.MATH))
    assert decision.reason is BudgetReason.CAPABILITY_NOT_IN_PLAN
    assert decision.permits_paid_call is False


@pytest.mark.parametrize(
    "outcome",
    [
        BudgetOutcome.REJECT_PLAN,
        BudgetOutcome.REJECT_QUOTA,
        BudgetOutcome.REJECT_SIZE,
        BudgetOutcome.REJECT_SYSTEM_BUDGET,
        BudgetOutcome.FEATURE_DISABLED,
        BudgetOutcome.REQUIRE_BRIEF,
        BudgetOutcome.REQUIRE_CONFIRMATION,
        BudgetOutcome.ALLOW_LOCAL_ONLY,
    ],
)
def test_no_non_allow_outcome_permits_spend(outcome: BudgetOutcome) -> None:
    assert outcome.permits_paid_call is False


# --- Tier selection -----------------------------------------------------------


def test_simple_non_stem_uses_cheapest_model() -> None:
    decision = decide(ctx(capability=CapabilityId.EXPLAIN_CONCEPT, difficulty=Difficulty.SIMPLE))
    assert decision.alias is ModelAlias.CHEAP_TEXT
    assert decision.outcome is BudgetOutcome.ALLOW_CHEAP_MODEL


def test_simple_stem_does_not_use_the_cheapest_model() -> None:
    """A short arithmetic question is still arithmetic; correctness matters."""
    decision = decide(ctx(capability=CapabilityId.MATH, difficulty=Difficulty.SIMPLE))
    assert decision.alias is ModelAlias.STANDARD_TUTOR


def test_moderate_uses_standard_tier() -> None:
    decision = decide(ctx(difficulty=Difficulty.MODERATE))
    assert decision.alias is ModelAlias.STANDARD_TUTOR
    assert decision.verifier_mode is VerifierMode.NONE


def test_advanced_uses_reasoning_tier_and_falls_back_downward() -> None:
    decision = decide(ctx(difficulty=Difficulty.ADVANCED))
    assert decision.alias is ModelAlias.ADVANCED_REASONING
    # Falling back UP would silently escalate cost on a struggling request.
    assert decision.fallback_alias is ModelAlias.STANDARD_TUTOR


def test_plan_without_advanced_stays_on_standard() -> None:
    plan = PlanPolicy(plan_code="LITE", allows_paid_ai=True, allows_advanced_model=False)
    decision = decide(ctx(plan=plan, difficulty=Difficulty.ADVANCED))
    assert decision.alias is ModelAlias.STANDARD_TUTOR


# --- Verifier selectivity -----------------------------------------------------


def test_simple_question_never_arms_the_verifier() -> None:
    decision = decide(ctx(capability=CapabilityId.EXPLAIN_CONCEPT, difficulty=Difficulty.SIMPLE))
    assert decision.verifier_mode is VerifierMode.NONE
    assert decision.verifier_alias is None


def test_advanced_stem_arms_verifier_conditionally_not_always() -> None:
    decision = decide(ctx(capability=CapabilityId.MATH, difficulty=Difficulty.ADVANCED))
    # Conditional, so a confident advanced answer still costs exactly one call.
    assert decision.verifier_mode is VerifierMode.ON_LOW_CONFIDENCE
    assert decision.verifier_alias is ModelAlias.VERIFIER_PRIMARY


def test_advanced_non_stem_does_not_arm_verifier() -> None:
    decision = decide(ctx(capability=CapabilityId.WRITING_FEEDBACK, difficulty=Difficulty.ADVANCED))
    assert decision.verifier_mode is VerifierMode.NONE


def test_high_stakes_always_verifies() -> None:
    decision = decide(ctx(high_stakes=True))
    assert decision.verifier_mode is VerifierMode.ALWAYS
    assert decision.reason is BudgetReason.HIGH_STAKES_MODE


def test_plan_can_forbid_the_verifier() -> None:
    plan = PlanPolicy(plan_code="LITE", allows_paid_ai=True, allows_verifier=False)
    decision = decide(ctx(plan=plan, capability=CapabilityId.MATH, difficulty=Difficulty.ADVANCED))
    assert decision.verifier_mode is VerifierMode.NONE


# --- Determinism --------------------------------------------------------------


def test_decision_is_deterministic() -> None:
    context = ctx(difficulty=Difficulty.ADVANCED)
    assert decide(context) == decide(context)


def test_retries_are_always_bounded() -> None:
    decision = decide(ctx())
    assert 1 <= decision.max_attempts <= 5
