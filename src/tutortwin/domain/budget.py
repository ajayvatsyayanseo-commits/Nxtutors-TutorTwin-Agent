"""Execution budget types - the vocabulary of the cost gate.

`ExecutionBudgetDecision` is the single object that decides whether a request may
reach a paid provider, which tier it may use, and whether a verifier is allowed.
Every rejection carries a machine-readable reason so tests assert on the reason,
not on an error string.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from tutortwin.domain.provider import ModelAlias


class BudgetOutcome(StrEnum):
    ALLOW_LOCAL_ONLY = "ALLOW_LOCAL_ONLY"
    ALLOW_CHEAP_MODEL = "ALLOW_CHEAP_MODEL"
    ALLOW_STANDARD = "ALLOW_STANDARD"
    ALLOW_ADVANCED = "ALLOW_ADVANCED"
    ALLOW_WITH_VERIFICATION = "ALLOW_WITH_VERIFICATION"
    REQUIRE_BRIEF = "REQUIRE_BRIEF"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    REJECT_PLAN = "REJECT_PLAN"
    REJECT_QUOTA = "REJECT_QUOTA"
    REJECT_SIZE = "REJECT_SIZE"
    REJECT_SYSTEM_BUDGET = "REJECT_SYSTEM_BUDGET"
    FEATURE_DISABLED = "FEATURE_DISABLED"

    @property
    def permits_paid_call(self) -> bool:
        """The single source of truth for 'may this request cost money'."""
        return self in _PAID_OUTCOMES


_PAID_OUTCOMES: frozenset[BudgetOutcome] = frozenset(
    {
        BudgetOutcome.ALLOW_CHEAP_MODEL,
        BudgetOutcome.ALLOW_STANDARD,
        BudgetOutcome.ALLOW_ADVANCED,
        BudgetOutcome.ALLOW_WITH_VERIFICATION,
    }
)


class VerifierMode(StrEnum):
    NONE = "NONE"
    ON_LOW_CONFIDENCE = "ON_LOW_CONFIDENCE"
    """Verifier runs only if the executor's confidence signals come back LOW."""

    ALWAYS = "ALWAYS"
    """Reserved for grading / test-generation modes where a wrong answer is costly."""


class BudgetReason(StrEnum):
    """Why the decision came out the way it did. Asserted directly in tests."""

    PLAN_DISALLOWS_AI = "PLAN_DISALLOWS_AI"
    FEATURE_FLAG_OFF = "FEATURE_FLAG_OFF"
    CAPABILITY_NOT_IN_PLAN = "CAPABILITY_NOT_IN_PLAN"
    DAILY_QUOTA_EXHAUSTED = "DAILY_QUOTA_EXHAUSTED"
    USER_BUDGET_EXHAUSTED = "USER_BUDGET_EXHAUSTED"
    USER_MONTHLY_BUDGET_EXHAUSTED = "USER_MONTHLY_BUDGET_EXHAUSTED"
    SYSTEM_BUDGET_EXHAUSTED = "SYSTEM_BUDGET_EXHAUSTED"
    SYSTEM_SPEND_VELOCITY = "SYSTEM_SPEND_VELOCITY"
    PROVIDER_BUDGET_EXHAUSTED = "PROVIDER_BUDGET_EXHAUSTED"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    PROVIDER_UNHEALTHY = "PROVIDER_UNHEALTHY"
    CAPABILITY_NOT_AVAILABLE = "CAPABILITY_NOT_AVAILABLE"
    ROUTINE_SIMPLE = "ROUTINE_SIMPLE"
    ROUTINE_MODERATE = "ROUTINE_MODERATE"
    ADVANCED_STEM = "ADVANCED_STEM"
    HIGH_STAKES_MODE = "HIGH_STAKES_MODE"


class ExecutionBudgetDecision(BaseModel):
    """Deterministic. Same inputs always produce the same decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: BudgetOutcome
    reason: BudgetReason
    alias: ModelAlias | None = None
    """None whenever the outcome forbids a paid call - a missing alias makes an
    accidental provider call a type error rather than a silent cost."""

    fallback_alias: ModelAlias | None = None
    max_output_tokens: int = Field(default=1024, ge=1, le=32_000)
    max_attempts: int = Field(default=2, ge=1, le=5)
    verifier_mode: VerifierMode = VerifierMode.NONE
    verifier_alias: ModelAlias | None = None

    @property
    def permits_paid_call(self) -> bool:
        return self.outcome.permits_paid_call and self.alias is not None


class QuotaSnapshot(BaseModel):
    """Counters read before the decision. Values are point-in-time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    calls_today: int = Field(default=0, ge=0)
    daily_call_limit: int | None = None
    spend_today_micros: int = Field(default=0, ge=0)
    user_daily_budget_micros: int | None = None
    spend_this_month_micros: int = Field(default=0, ge=0)
    user_monthly_budget_micros: int | None = None

    system_spend_today_micros: int = Field(default=0, ge=0)
    system_daily_budget_micros: int | None = None

    system_spend_last_hour_micros: int = Field(default=0, ge=0)
    system_hourly_budget_micros: int | None = None
    """Spend *velocity*. A daily ceiling alone lets a retry loop burn the day's
    budget in four minutes and only report it afterwards."""

    provider_spend_today_micros: int = Field(default=0, ge=0)
    provider_daily_budget_micros: int | None = None
    """Per vendor, so one provider's runaway cannot eat the other's headroom and
    leave the service with no working fallback."""

    spend_by_provider_micros: dict[str, int] = Field(default_factory=dict)
    failures_by_provider: dict[str, int] = Field(default_factory=dict)
    """Every vendor, not just one. The tier decision happens before an alias is
    resolved to a vendor, so the snapshot cannot know which vendor to ask about -
    it carries them all and the gateway, which *does* know the mapping, picks."""

    recent_provider_failures: int = Field(default=0, ge=0)

    # --- media allowances, counted from rows the pipeline already writes ------
    pdf_pages_today: int = Field(default=0, ge=0)
    daily_pdf_page_limit: int | None = None
    ocr_pages_today: int = Field(default=0, ge=0)
    daily_ocr_page_limit: int | None = None
    voice_seconds_today: int = Field(default=0, ge=0)
    daily_voice_second_limit: int | None = None
    mocks_today: int = Field(default=0, ge=0)
    daily_mock_limit: int | None = None

    @property
    def daily_calls_exhausted(self) -> bool:
        return self.daily_call_limit is not None and self.calls_today >= self.daily_call_limit

    @property
    def user_budget_exhausted(self) -> bool:
        return (
            self.user_daily_budget_micros is not None
            and self.spend_today_micros >= self.user_daily_budget_micros
        )

    @property
    def monthly_budget_exhausted(self) -> bool:
        return (
            self.user_monthly_budget_micros is not None
            and self.spend_this_month_micros >= self.user_monthly_budget_micros
        )

    @property
    def system_budget_exhausted(self) -> bool:
        return (
            self.system_daily_budget_micros is not None
            and self.system_spend_today_micros >= self.system_daily_budget_micros
        )

    @property
    def spend_velocity_exceeded(self) -> bool:
        return (
            self.system_hourly_budget_micros is not None
            and self.system_spend_last_hour_micros >= self.system_hourly_budget_micros
        )

    @property
    def provider_budget_exhausted(self) -> bool:
        return (
            self.provider_daily_budget_micros is not None
            and self.provider_spend_today_micros >= self.provider_daily_budget_micros
        )

    def blocked_providers(
        self, *, daily_budget_micros: int | None, failure_circuit: int
    ) -> frozenset[str]:
        """Vendors this request must not be sent to.

        Two independent reasons, both meaning "stop paying this one":

        * **over budget** - it has spent its daily allowance, and continuing
          would let it consume the headroom the fallback needs.
        * **failing** - `failure_circuit` consecutive calls produced no output
          and no charge, so it is down and each retry is latency for nothing.

        Returning a set rather than a boolean is what lets the gateway fall
        through to a healthy vendor instead of refusing the request outright.
        """
        blocked = {
            name for name, count in self.failures_by_provider.items() if count >= failure_circuit
        }
        if daily_budget_micros is not None:
            blocked |= {
                name
                for name, spend in self.spend_by_provider_micros.items()
                if spend >= daily_budget_micros
            }
        return frozenset(blocked)

    def media_allowance_exceeded(
        self, *, pdf_pages: int = 0, ocr_pages: int = 0, voice_seconds: int = 0, mocks: int = 0
    ) -> str | None:
        """Would this much extra work cross a per-student daily ceiling?

        Returns the name of the ceiling that would be crossed, or None. Checked
        *before* the work rather than after, because a page already read by
        vision has already been paid for.
        """
        checks = (
            ("pdf_pages", self.pdf_pages_today + pdf_pages, self.daily_pdf_page_limit),
            ("ocr_pages", self.ocr_pages_today + ocr_pages, self.daily_ocr_page_limit),
            (
                "voice_seconds",
                self.voice_seconds_today + voice_seconds,
                self.daily_voice_second_limit,
            ),
            ("mocks", self.mocks_today + mocks, self.daily_mock_limit),
        )
        for name, projected, limit in checks:
            if limit is not None and projected > limit:
                return name
        return None
