"""Model catalog reads and per-call usage accounting.

Two jobs live here, both of them the reason no vendor model ID appears in
business code:

* resolve an alias to the vendor model *and the price that applied at read time*
* write exactly one usage_ledger row per provider call

The price is captured with the resolution, not looked up again at write time, so
a catalog edit mid-request cannot make the ledger disagree with what was charged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.learning_models import Assessment
from tutortwin.db.models import (
    FeatureFlag,
    MediaExtraction,
    MediaObject,
    ModelCatalog,
    UsageLedger,
)
from tutortwin.domain.budget import QuotaSnapshot
from tutortwin.domain.provider import ModelAlias, ModelCall, ModelCatalogEntry, Provider


async def load_catalog(session: AsyncSession) -> dict[ModelAlias, ModelCatalogEntry]:
    """Active catalog, keyed by alias.

    Loaded once per request rather than per call: a single request may make a
    primary call plus a verifier call, and they must price against the same rows.
    """
    rows = (
        await session.execute(
            select(ModelCatalog)
            .where(ModelCatalog.is_active.is_(True))
            .order_by(ModelCatalog.model_alias, ModelCatalog.created_at.desc())
        )
    ).scalars()

    catalog: dict[ModelAlias, ModelCatalogEntry] = {}
    for row in rows:
        try:
            alias = ModelAlias(row.model_alias)
            provider = Provider(row.provider)
        except ValueError:
            # An unknown alias/provider in the table is config drift, not a crash:
            # skip it so one bad admin row cannot take the service down.
            continue
        if alias in catalog:
            continue  # first row wins - ordered newest-active-first above
        catalog[alias] = ModelCatalogEntry(
            alias=alias,
            provider=provider,
            model_id=row.model_id,
            input_cost_micros_per_1k=row.input_cost_micros_per_1k,
            output_cost_micros_per_1k=row.output_cost_micros_per_1k,
            rate_version=row.rate_version,
        )
    return catalog


async def record_model_call(
    session: AsyncSession,
    *,
    call: ModelCall,
    subject_id: UUID | None,
    request_event_id: UUID | None,
    capability: str | None = None,
) -> None:
    """One ledger row per provider call, including failed attempts.

    Failed calls are recorded because a timeout can still consume input tokens,
    and because an unrecorded failure is how a retry storm stays invisible.
    """
    session.add(
        UsageLedger(
            subject_id=subject_id,
            request_event_id=request_event_id,
            provider=str(call.provider),
            model_alias=str(call.alias),
            model_id=call.model_id,
            capability=capability,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cached_tokens=call.cached_tokens,
            estimated_cost_micros=call.estimated_cost_micros,
            rate_version=call.rate_version,
        )
    )


async def load_quota_snapshot(  # noqa: PLR0913 - every ceiling is a distinct limit
    session: AsyncSession,
    *,
    subject_id: UUID,
    now: datetime,
    daily_call_limit: int | None,
    user_daily_budget_micros: int | None,
    user_monthly_budget_micros: int | None = None,
    system_daily_budget_micros: int | None = None,
    system_hourly_budget_micros: int | None = None,
    provider: Provider | None = None,
    provider_daily_budget_micros: int | None = None,
    failure_window_minutes: int = 10,
    daily_pdf_page_limit: int | None = None,
    daily_ocr_page_limit: int | None = None,
    daily_voice_second_limit: int | None = None,
    daily_mock_limit: int | None = None,
) -> QuotaSnapshot:
    """Read the counters every ceiling is measured against.

    Deliberately a read, not a reservation. Two concurrent requests can both see
    the same count and both proceed, so the limit is soft by at most the number
    of in-flight requests. A hard reservation would need a row lock held across
    the provider call, which is exactly what the serverless design forbids.

    Everything here is derived from rows the system already wrote for its own
    reasons - `usage_ledger`, `media_objects`, `media_extractions`,
    `assessments`. Nothing is a separately maintained counter, because a counter
    that drifts from the ledger is worse than no counter at all.

    Three statements, not ten. These run on the hot path of every request, and
    each extra round trip to Neon is latency paid on every message.
    """
    day_start = now.astimezone(UTC) - timedelta(days=1)
    hour_start = now.astimezone(UTC) - timedelta(hours=1)
    month_start = now.astimezone(UTC) - timedelta(days=30)
    failure_start = now.astimezone(UTC) - timedelta(minutes=failure_window_minutes)
    provider_name = str(provider) if provider else ""

    mine = UsageLedger.subject_id == subject_id
    cost = UsageLedger.estimated_cost_micros

    # **Retry accounting.** Every attempt is written to the ledger, including the
    # ones that failed - that is what makes a retry storm visible. But an attempt
    # that never reached a working provider must not consume the student's daily
    # call allowance: three failed retries would otherwise cost them three calls
    # for an answer they never received.
    #
    # "Reached a provider" is read from the evidence rather than asserted: a call
    # that produced output, or that the vendor billed for, counts. A call with
    # neither does not. So a timeout that still billed input tokens is charged
    # (because it was), and a connection refused is not (because it was not).
    billed = (UsageLedger.output_tokens > 0) | (cost > 0)

    row = (
        await session.execute(
            select(
                func.count(UsageLedger.id).filter(
                    mine & (UsageLedger.created_at >= day_start) & billed
                ),
                func.coalesce(
                    func.sum(cost).filter(mine & (UsageLedger.created_at >= day_start)), 0
                ),
                func.coalesce(
                    func.sum(cost).filter(mine & (UsageLedger.created_at >= month_start)), 0
                ),
                func.coalesce(func.sum(cost).filter(UsageLedger.created_at >= day_start), 0),
                func.coalesce(func.sum(cost).filter(UsageLedger.created_at >= hour_start), 0),
                func.coalesce(
                    func.sum(cost).filter(
                        (UsageLedger.created_at >= day_start)
                        & (UsageLedger.provider == provider_name)
                    ),
                    0,
                ),
                # A failed attempt is a ledger row with no output and no cost, so
                # the circuit breaker reads the same evidence as the bill.
                func.count(UsageLedger.id).filter(
                    (UsageLedger.created_at >= failure_start)
                    & (UsageLedger.output_tokens == 0)
                    & (cost == 0)
                    & (UsageLedger.provider == provider_name)
                ),
            ).where(UsageLedger.created_at >= month_start)
        )
    ).one()

    # Spend and failures per vendor, for every vendor at once. The tier decision
    # runs before an alias is resolved to a vendor, so asking about one vendor
    # here would mean asking about the wrong one.
    per_provider = (
        await session.execute(
            select(
                UsageLedger.provider,
                func.coalesce(func.sum(cost).filter(UsageLedger.created_at >= day_start), 0),
                func.count(UsageLedger.id).filter(
                    (UsageLedger.created_at >= failure_start)
                    & (UsageLedger.output_tokens == 0)
                    & (cost == 0)
                ),
            )
            .where(UsageLedger.created_at >= day_start)
            .group_by(UsageLedger.provider)
        )
    ).all()

    media = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(MediaObject.page_count).filter(MediaObject.kind == "PDF"), 0
                ),
                func.coalesce(
                    func.sum(MediaObject.duration_seconds).filter(MediaObject.kind == "AUDIO"), 0
                ),
            ).where(MediaObject.subject_id == subject_id, MediaObject.created_at >= day_start)
        )
    ).one()

    # OCR and vision both cost something; a page read straight from digital text
    # does not, so it is not counted against an OCR allowance.
    counts = (
        await session.execute(
            select(
                select(func.count())
                .select_from(MediaExtraction)
                .where(
                    MediaExtraction.subject_id == subject_id,
                    MediaExtraction.created_at >= day_start,
                    MediaExtraction.method.in_(("LOCAL_OCR", "VISION")),
                )
                .scalar_subquery(),
                select(func.count())
                .select_from(Assessment)
                .where(
                    Assessment.subject_id == subject_id,
                    Assessment.created_at >= day_start,
                    Assessment.kind == "MOCK_TEST",
                )
                .scalar_subquery(),
            )
        )
    ).one()

    return QuotaSnapshot(
        calls_today=int(row[0] or 0),
        daily_call_limit=daily_call_limit,
        spend_today_micros=int(row[1] or 0),
        user_daily_budget_micros=user_daily_budget_micros,
        spend_this_month_micros=int(row[2] or 0),
        user_monthly_budget_micros=user_monthly_budget_micros,
        system_spend_today_micros=int(row[3] or 0),
        system_daily_budget_micros=system_daily_budget_micros,
        system_spend_last_hour_micros=int(row[4] or 0),
        system_hourly_budget_micros=system_hourly_budget_micros,
        provider_spend_today_micros=int(row[5] or 0),
        provider_daily_budget_micros=provider_daily_budget_micros,
        spend_by_provider_micros={str(r[0]): int(r[1] or 0) for r in per_provider},
        failures_by_provider={str(r[0]): int(r[2] or 0) for r in per_provider},
        recent_provider_failures=int(row[6] or 0) if provider else 0,
        pdf_pages_today=int(media[0] or 0),
        daily_pdf_page_limit=daily_pdf_page_limit,
        ocr_pages_today=int(counts[0] or 0),
        daily_ocr_page_limit=daily_ocr_page_limit,
        voice_seconds_today=int(media[1] or 0),
        daily_voice_second_limit=daily_voice_second_limit,
        mocks_today=int(counts[1] or 0),
        daily_mock_limit=daily_mock_limit,
    )


async def load_feature_flags(session: AsyncSession) -> dict[str, bool]:
    rows = (await session.execute(select(FeatureFlag))).scalars()
    return {row.key: row.enabled for row in rows}
