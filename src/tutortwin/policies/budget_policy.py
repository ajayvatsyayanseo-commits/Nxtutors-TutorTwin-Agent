"""The cost-aware model router.

A pure function: `(plan, capability, difficulty, context size, quota, flags) ->
ExecutionBudgetDecision`. No I/O, no clock, no randomness, so every routing
choice is unit-testable by construction.

Precedence is the whole design. Cheap, certain rejections run before expensive,
uncertain ones, and every gate that can forbid spending runs before any gate that
selects a tier. That ordering is what makes "ineligible student costs nothing" a
property of the code rather than a promise.
"""

from __future__ import annotations

from dataclasses import dataclass

from tutortwin.domain.budget import (
    BudgetOutcome,
    BudgetReason,
    ExecutionBudgetDecision,
    QuotaSnapshot,
    VerifierMode,
)
from tutortwin.domain.capabilities import (
    STEM_CAPABILITIES,
    TEXT_CAPABILITIES,
    CapabilityId,
    Difficulty,
)
from tutortwin.domain.provider import ModelAlias

# Context ceiling. Beyond this the request is rejected rather than silently
# truncated - a silently truncated prompt produces a confidently wrong answer.
MAX_CONTEXT_TOKENS = 24_000

# Consecutive provider failures after which we stop paying to retry a sick vendor.
PROVIDER_FAILURE_CIRCUIT = 5

_OUTCOME_FOR_ALIAS: dict[ModelAlias, BudgetOutcome] = {
    ModelAlias.CHEAP_TEXT: BudgetOutcome.ALLOW_CHEAP_MODEL,
    ModelAlias.STANDARD_TUTOR: BudgetOutcome.ALLOW_STANDARD,
    ModelAlias.ADVANCED_REASONING: BudgetOutcome.ALLOW_ADVANCED,
}


@dataclass(frozen=True, slots=True)
class SystemLimits:
    """The platform's own ceilings, above whatever any plan allows.

    A plan bounds one student. These bound the bill: they are the difference
    between "a student cannot spend more than $2 today" and "the service cannot
    spend more than $50 today, however many students there are".
    """

    daily_budget_micros: int | None = None
    hourly_budget_micros: int | None = None
    provider_daily_budget_micros: int | None = None
    failure_circuit: int = PROVIDER_FAILURE_CIRCUIT


DEFAULT_SYSTEM_LIMITS = SystemLimits()


@dataclass(frozen=True, slots=True)
class PlanPolicy:
    """Per-plan limits. Loaded from `plan_policies`; defaults are conservative."""

    plan_code: str
    allows_paid_ai: bool
    allowed_capabilities: frozenset[CapabilityId] | None = None
    """None means 'all text capabilities'."""

    daily_call_limit: int | None = None
    user_daily_budget_micros: int | None = None
    user_monthly_budget_micros: int | None = None
    allows_advanced_model: bool = True
    allows_verifier: bool = True
    max_output_tokens: int = 1024

    # Per-student media ceilings. Counted from the rows the pipeline writes, so
    # none of these is a separate counter that can drift from what happened.
    daily_pdf_page_limit: int | None = None
    daily_ocr_page_limit: int | None = None
    daily_voice_second_limit: int | None = None
    daily_mock_limit: int | None = None


FREE_PLAN = PlanPolicy(plan_code="FREE", allows_paid_ai=False)

# --- what one subscription can be allowed to cost ----------------------------
#
# A per-student ceiling only protects the business if it sits BELOW the revenue
# that student brings in. These were $2/day and $20/month against a Rs 100
# (~$1.20) subscription - roughly 17x the price - so no student could ever hit
# them before the account had lost money many times over. A ceiling above
# revenue is decoration.
#
# Derived from the price rather than typed as a literal, so changing the plan
# price cannot silently leave the ceilings behind.
SUBSCRIPTION_PRICE_USD_MICROS = 1_200_000
"""Rs 100/month at roughly Rs 83/USD. Approximate on purpose: this is a safety
ceiling, not an invoice, and a 10% FX move must not change who gets served."""

MODEL_SPEND_SHARE = 0.45
"""Share of a subscription that may go to model providers. The rest covers
infrastructure, payment fees and margin. Raise it knowingly - at 1.0 the product
breaks even before a single fixed cost."""

_MONTHLY_CEILING = int(SUBSCRIPTION_PRICE_USD_MICROS * MODEL_SPEND_SHARE)

_DAILY_CEILING = int(_MONTHLY_CEILING * 0.35)
"""Deliberately NOT monthly/30. A student revising the night before an exam
should be able to spend a third of their month's allowance in one day; the
monthly figure is the real limit and this one only stops a single day running
away."""

PRO_PLAN = PlanPolicy(
    plan_code="PRO",
    allows_paid_ai=True,
    daily_call_limit=200,
    user_daily_budget_micros=_DAILY_CEILING,
    user_monthly_budget_micros=_MONTHLY_CEILING,
    allows_advanced_model=True,
    allows_verifier=True,
    max_output_tokens=2048,
    daily_pdf_page_limit=100,
    daily_ocr_page_limit=60,
    daily_voice_second_limit=900,
    daily_mock_limit=5,
)


@dataclass(frozen=True, slots=True)
class BudgetContext:
    """Everything the decision depends on. Explicit so the function stays pure."""

    plan: PlanPolicy
    capability: CapabilityId
    difficulty: Difficulty
    estimated_context_tokens: int
    quota: QuotaSnapshot
    ai_enabled: bool = True
    """Global kill switch, from `feature_flags`."""

    high_stakes: bool = False
    """Grading / mock-test generation, where a wrong answer is expensive."""


def decide(ctx: BudgetContext) -> ExecutionBudgetDecision:
    """Deterministic routing decision.

    Order below is load-bearing; see module docstring.
    """
    # 1. Global kill switch. Cheapest possible check, and an operator pulling it
    #    must beat every other consideration.
    if not ctx.ai_enabled:
        return _reject(BudgetOutcome.FEATURE_DISABLED, BudgetReason.FEATURE_FLAG_OFF)

    # 2. Plan entitlement. The primary cost gate.
    if not ctx.plan.allows_paid_ai:
        return _reject(BudgetOutcome.REJECT_PLAN, BudgetReason.PLAN_DISALLOWS_AI)

    # 3. Capability must be executable at all in this phase.
    if ctx.capability not in TEXT_CAPABILITIES:
        return _reject(BudgetOutcome.FEATURE_DISABLED, BudgetReason.CAPABILITY_NOT_AVAILABLE)

    # 4. Capability must be in the plan's matrix.
    allowed = ctx.plan.allowed_capabilities
    if allowed is not None and ctx.capability not in allowed:
        return _reject(BudgetOutcome.REJECT_PLAN, BudgetReason.CAPABILITY_NOT_IN_PLAN)

    # 5. Budgets before quotas: a blown system budget affects everyone and must
    #    fail closed even for a student who still has personal quota left.
    #
    #    Velocity is checked before the daily total, because it is the one that
    #    catches a runaway *while it is running*. A daily ceiling alone reports
    #    the fire after the building has burned.
    if ctx.quota.spend_velocity_exceeded:
        return _reject(BudgetOutcome.REJECT_SYSTEM_BUDGET, BudgetReason.SYSTEM_SPEND_VELOCITY)
    if ctx.quota.system_budget_exhausted:
        return _reject(BudgetOutcome.REJECT_SYSTEM_BUDGET, BudgetReason.SYSTEM_BUDGET_EXHAUSTED)
    if ctx.quota.provider_budget_exhausted:
        return _reject(BudgetOutcome.REJECT_SYSTEM_BUDGET, BudgetReason.PROVIDER_BUDGET_EXHAUSTED)
    if ctx.quota.monthly_budget_exhausted:
        return _reject(BudgetOutcome.REJECT_QUOTA, BudgetReason.USER_MONTHLY_BUDGET_EXHAUSTED)
    if ctx.quota.user_budget_exhausted:
        return _reject(BudgetOutcome.REJECT_QUOTA, BudgetReason.USER_BUDGET_EXHAUSTED)
    if ctx.quota.daily_calls_exhausted:
        return _reject(BudgetOutcome.REJECT_QUOTA, BudgetReason.DAILY_QUOTA_EXHAUSTED)

    # 6. Provider health. Paying to retry a failing vendor burns money for nothing.
    if ctx.quota.recent_provider_failures >= PROVIDER_FAILURE_CIRCUIT:
        return _reject(BudgetOutcome.REJECT_SYSTEM_BUDGET, BudgetReason.PROVIDER_UNHEALTHY)

    # 7. Size. Reject rather than truncate.
    if ctx.estimated_context_tokens > MAX_CONTEXT_TOKENS:
        return _reject(BudgetOutcome.REJECT_SIZE, BudgetReason.CONTEXT_TOO_LARGE)

    # --- Past this line the request may spend money. Now pick the cheapest tier.
    return _select_tier(ctx)


def _select_tier(ctx: BudgetContext) -> ExecutionBudgetDecision:
    is_stem = ctx.capability in STEM_CAPABILITIES

    if ctx.difficulty is Difficulty.ADVANCED and ctx.plan.allows_advanced_model:
        alias = ModelAlias.ADVANCED_REASONING
        reason = BudgetReason.ADVANCED_STEM
    elif ctx.difficulty is Difficulty.SIMPLE and not is_stem:
        # Definitions and vocabulary do not need a reasoning model.
        alias = ModelAlias.CHEAP_TEXT
        reason = BudgetReason.ROUTINE_SIMPLE
    else:
        alias = ModelAlias.STANDARD_TUTOR
        reason = BudgetReason.ROUTINE_MODERATE

    # Verifier policy: never on by default. Enabled only where a wrong answer is
    # expensive, and even then it is conditional on LOW confidence at runtime -
    # so a confident advanced answer still costs exactly one call.
    verifier_mode = VerifierMode.NONE
    verifier_alias: ModelAlias | None = None
    if ctx.plan.allows_verifier:
        if ctx.high_stakes:
            verifier_mode = VerifierMode.ALWAYS
            verifier_alias = ModelAlias.VERIFIER_PRIMARY
            reason = BudgetReason.HIGH_STAKES_MODE
        elif is_stem and ctx.difficulty is Difficulty.ADVANCED:
            verifier_mode = VerifierMode.ON_LOW_CONFIDENCE
            verifier_alias = ModelAlias.VERIFIER_PRIMARY

    outcome = (
        BudgetOutcome.ALLOW_WITH_VERIFICATION
        if verifier_mode is not VerifierMode.NONE
        else _OUTCOME_FOR_ALIAS[alias]
    )

    return ExecutionBudgetDecision(
        outcome=outcome,
        reason=reason,
        alias=alias,
        # Fall back one tier down, not up: a struggling primary should not
        # silently escalate cost.
        fallback_alias=(
            ModelAlias.STANDARD_TUTOR if alias is ModelAlias.ADVANCED_REASONING else None
        ),
        max_output_tokens=ctx.plan.max_output_tokens,
        max_attempts=2,
        verifier_mode=verifier_mode,
        verifier_alias=verifier_alias,
    )


def _reject(outcome: BudgetOutcome, reason: BudgetReason) -> ExecutionBudgetDecision:
    """A rejection never carries an alias, so it cannot accidentally be spent."""
    return ExecutionBudgetDecision(outcome=outcome, reason=reason, alias=None)
