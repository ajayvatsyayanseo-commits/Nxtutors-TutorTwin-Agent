"""The ceilings, and the order they fire in.

A cost control is only worth what its *precedence* is worth. Every gate here can
forbid spending, and the order decides which reason an operator sees when two are
true at once - which is the difference between "the platform is over budget" and
"this student is over budget", and therefore between paging someone and not.

These are pure-function tests: the policy takes a snapshot and returns a
decision, with no clock, no database and no randomness.
"""

from __future__ import annotations

import pytest

from tutortwin.domain.budget import BudgetOutcome, BudgetReason, QuotaSnapshot
from tutortwin.domain.capabilities import CapabilityId, Difficulty
from tutortwin.policies.budget_policy import PRO_PLAN, BudgetContext, decide


def context(quota: QuotaSnapshot, **kwargs: object) -> BudgetContext:
    return BudgetContext(
        plan=kwargs.pop("plan", PRO_PLAN),  # type: ignore[arg-type]
        capability=CapabilityId.GENERAL_TUTORING,
        difficulty=Difficulty.MODERATE,
        estimated_context_tokens=500,
        quota=quota,
        **kwargs,  # type: ignore[arg-type]
    )


def test_spend_velocity_stops_a_runaway_before_the_daily_ceiling_notices() -> None:
    """The hour ceiling is the one that catches a loop while it is looping.

    A daily budget alone is an epitaph: a retry storm can spend the whole day's
    money in four minutes, and the daily gate only fires once it already has.
    """
    quota = QuotaSnapshot(
        system_spend_last_hour_micros=10_000_000,
        system_hourly_budget_micros=10_000_000,
        # The day is nowhere near its ceiling. That is the point.
        system_spend_today_micros=10_000_000,
        system_daily_budget_micros=50_000_000,
    )

    decision = decide(context(quota))

    assert decision.outcome is BudgetOutcome.REJECT_SYSTEM_BUDGET
    assert decision.reason is BudgetReason.SYSTEM_SPEND_VELOCITY
    assert decision.alias is None, "a rejection must carry no alias to spend"


def test_a_blown_system_budget_beats_a_student_who_still_has_quota() -> None:
    quota = QuotaSnapshot(
        system_spend_today_micros=50_000_000,
        system_daily_budget_micros=50_000_000,
        spend_today_micros=0,
        user_daily_budget_micros=2_000_000,
    )

    decision = decide(context(quota))

    assert decision.reason is BudgetReason.SYSTEM_BUDGET_EXHAUSTED


def test_one_provider_running_away_does_not_consume_the_other_headroom() -> None:
    """Per-vendor ceilings keep a fallback available.

    Without them, one provider's runaway spends the whole system budget and the
    service is left with no working model at all - the fallback is refused for a
    budget the fallback did not spend.
    """
    quota = QuotaSnapshot(
        provider_spend_today_micros=30_000_000,
        provider_daily_budget_micros=30_000_000,
        system_spend_today_micros=30_000_000,
        system_daily_budget_micros=50_000_000,
    )

    decision = decide(context(quota))

    assert decision.reason is BudgetReason.PROVIDER_BUDGET_EXHAUSTED


def test_the_monthly_ceiling_catches_what_thirty_daily_ceilings_do_not() -> None:
    """A student inside their daily budget every day is still a monthly bill.

    $2/day for 30 days is $60. The monthly cap is deliberately not 30x the daily
    one, because the daily cap exists to bound a bad day, not to be a licence.
    """
    quota = QuotaSnapshot(
        spend_today_micros=100_000,
        user_daily_budget_micros=2_000_000,
        spend_this_month_micros=20_000_000,
        user_monthly_budget_micros=20_000_000,
    )

    decision = decide(context(quota))

    assert decision.outcome is BudgetOutcome.REJECT_QUOTA
    assert decision.reason is BudgetReason.USER_MONTHLY_BUDGET_EXHAUSTED


def test_a_sick_provider_stops_being_paid_to_fail() -> None:
    quota = QuotaSnapshot(recent_provider_failures=5)

    decision = decide(context(quota))

    assert decision.reason is BudgetReason.PROVIDER_UNHEALTHY
    assert not decision.permits_paid_call


def test_headroom_everywhere_still_permits_a_paid_call() -> None:
    """The gates must not be so eager that a healthy request is refused."""
    quota = QuotaSnapshot(
        calls_today=1,
        daily_call_limit=200,
        spend_today_micros=1_000,
        user_daily_budget_micros=2_000_000,
        spend_this_month_micros=1_000,
        user_monthly_budget_micros=20_000_000,
        system_spend_today_micros=1_000,
        system_daily_budget_micros=50_000_000,
        system_spend_last_hour_micros=1_000,
        system_hourly_budget_micros=10_000_000,
        provider_spend_today_micros=1_000,
        provider_daily_budget_micros=30_000_000,
    )

    decision = decide(context(quota))

    assert decision.permits_paid_call
    assert decision.alias is not None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"pdf_pages": 1}, "pdf_pages"),
        ({"ocr_pages": 1}, "ocr_pages"),
        ({"voice_seconds": 1}, "voice_seconds"),
        ({"mocks": 1}, "mocks"),
    ],
)
def test_media_allowances_are_checked_before_the_work_not_after(
    kwargs: dict[str, int], expected: str
) -> None:
    """Projected, not current.

    A page already read by vision has already been paid for; refusing afterwards
    protects nothing. The check asks "would this cross the line", which is the
    only version that can prevent the spend.
    """
    quota = QuotaSnapshot(
        pdf_pages_today=100,
        daily_pdf_page_limit=100,
        ocr_pages_today=60,
        daily_ocr_page_limit=60,
        voice_seconds_today=900,
        daily_voice_second_limit=900,
        mocks_today=5,
        daily_mock_limit=5,
    )

    assert quota.media_allowance_exceeded(**kwargs) == expected


def test_media_allowances_permit_work_that_fits() -> None:
    quota = QuotaSnapshot(
        pdf_pages_today=10,
        daily_pdf_page_limit=100,
        ocr_pages_today=5,
        daily_ocr_page_limit=60,
    )

    assert quota.media_allowance_exceeded(pdf_pages=20, ocr_pages=10) is None


def test_an_unset_ceiling_is_no_ceiling_not_a_zero_one() -> None:
    """None must mean "unlimited", never "zero".

    A None read as 0 turns an unconfigured environment into one that refuses
    every request, which looks exactly like a broken deployment.
    """
    quota = QuotaSnapshot(
        spend_today_micros=999_999_999,
        system_spend_today_micros=999_999_999,
        spend_this_month_micros=999_999_999,
        pdf_pages_today=10_000,
    )

    assert not quota.user_budget_exhausted
    assert not quota.system_budget_exhausted
    assert not quota.monthly_budget_exhausted
    assert not quota.spend_velocity_exceeded
    assert not quota.provider_budget_exhausted
    assert quota.media_allowance_exceeded(pdf_pages=10_000) is None


def test_a_psycopg2_dsn_is_rejected_with_a_useful_message() -> None:
    """The wrong driver must not surface as a missing module.

    `postgresql+psycopg2://` is the shape most other services use, and it fails
    eight frames deep inside SQLAlchemy with "No module named 'psycopg2'" -
    which reads like a broken install and sends the reader to pip instead of to
    the URL.
    """
    import pytest as _pytest

    from tutortwin.config import Settings

    with _pytest.raises(ValueError, match="psycopg 3"):
        Settings(
            environment="test",
            database_url="postgresql+psycopg2://u:p@host:5432/db",  # type: ignore[arg-type]
        )

    # The supported form, and the bare form SQLAlchemy resolves itself.
    Settings(
        environment="test",
        database_url="postgresql+psycopg://u:p@host:5432/db",  # type: ignore[arg-type]
    )
    Settings(
        environment="test",
        database_url="postgresql://u:p@host:5432/db",  # type: ignore[arg-type]
    )


def test_the_search_path_has_no_space_after_the_comma() -> None:
    """It travels inside libpq's `options` string, where a space splits arguments.

    With a space the server receives `search_path=tutor_twin,` and refuses the
    connection outright with "List syntax is invalid". A `SET search_path`
    statement tolerates it, so migrations succeed while the application cannot
    open a single connection - which is a confusing way to find out.
    """
    from tutortwin.config import Settings

    shared = Settings(environment="test", database_postgres_schema="tutor_twin")
    assert shared.search_path == "tutor_twin,public"
    assert " " not in shared.search_path

    # A database TutorTwin owns outright needs no second entry. Stated
    # explicitly rather than relying on the default: Settings reads .env, so a
    # bare construction picks up whatever this machine happens to be pointed at.
    assert Settings(environment="test", database_postgres_schema="public").search_path == "public"


def test_a_malformed_schema_name_is_refused() -> None:
    """The schema name reaches SQL as an identifier, never as a bound parameter."""
    import pytest as _pytest

    from tutortwin.config import Settings

    for bad in ('tutor";drop schema public cascade;--', "Tutor Twin", "1twin", ""):
        with _pytest.raises(ValueError, match="lowercase identifier"):
            Settings(environment="test", database_postgres_schema=bad)


def test_a_blank_credential_is_unset_not_empty() -> None:
    """`TUTORTWIN_OPENAI_API_KEY=` must mean "not configured".

    Read literally it is a *present* credential of zero length, so the vendor
    client is constructed and raises at import - the service refuses to boot
    because of a variable nobody filled in. That is a whole deployment lost to a
    trailing `=`.
    """
    from tutortwin.config import Settings

    blank = Settings(
        environment="test",
        openai_api_key="",  # type: ignore[arg-type]
        anthropic_api_key="   ",  # type: ignore[arg-type]
        r2_account_id="",
        r2_bucket="  ",
        tasks_queue="",
    )
    assert blank.openai_api_key is None
    assert blank.anthropic_api_key is None
    assert blank.r2_account_id is None
    # All-or-nothing groups must not count a blank as present, or a deployment
    # starts believing it has object storage.
    assert not blank.r2_configured
    assert not blank.tasks_configured

    # A real value still survives.
    real = Settings(environment="test", openai_api_key="sk-real")  # type: ignore[arg-type]
    assert real.openai_api_key is not None
    assert real.openai_api_key.get_secret_value() == "sk-real"
