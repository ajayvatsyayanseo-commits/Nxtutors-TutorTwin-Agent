"""Dashboard, cost analysis and the audit log.

Every number here is an aggregate over rows the system already wrote for its own
reasons — `usage_ledger` for spend, `request_states` for failures, `media_objects`
for extraction routes. Nothing is a separately-maintained counter, because a
counter that drifts from the ledger is worse than no counter.

Costs are reported in **micros** (millionths of a unit) exactly as stored. Money
is never carried as a float, and the rate that applied at call time is stored on
the row, so a price change does not silently rewrite history.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, String, and_, case, cast, func, select

from tutortwin.api.admin_deps import DbSession, require
from tutortwin.db.admin_models import AdminUser
from tutortwin.db.knowledge_models import DocumentChunk, KnowledgeSource
from tutortwin.db.learning_models import Assessment, AssessmentAttempt
from tutortwin.db.models import (
    AuditEvent,
    Conversation,
    Job,
    MediaExtraction,
    MediaObject,
    Message,
    RequestEvent,
    RequestState,
    Subject,
    Tutor,
    TutorAssignment,
    UsageLedger,
)
from tutortwin.domain.admin import AdminActor, Permission
from tutortwin.observability.logging import get_logger
from tutortwin.repositories import admin as admin_repo

router = APIRouter()
logger = get_logger(__name__)

WindowDays = Annotated[int, Query(ge=1, le=365)]


class DashboardCounts(BaseModel):
    students_total: int
    students_active_in_window: int
    requests: int
    requests_failed: int
    questions_answered: int
    media_objects: int
    mock_tests: int
    model_calls: int
    cost_micros: int
    verifier_calls: int
    quota_blocked: int
    jobs_failed: int
    extraction_cache_entries: int
    local_extractions: int
    vision_escalations: int
    rag_sources: int
    rag_chunks: int
    input_tokens: int
    cached_input_tokens: int
    latency_p50_ms: int | None
    latency_p95_ms: int | None


class DashboardResponse(BaseModel):
    window_days: int
    generated_at: datetime
    counts: DashboardCounts
    cost_by_alias: list[dict[str, Any]]
    failures_by_code: list[dict[str, Any]]


async def _scalar(db: DbSession, statement: Select[Any]) -> int:
    return int((await db.execute(statement)).scalar_one() or 0)


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(
    db: DbSession,
    window_days: WindowDays = 7,
    _: AdminActor = Depends(require(Permission.DASHBOARD_READ)),
) -> DashboardResponse:
    since = admin_repo.window_start(window_days)

    students_total = await _scalar(db, select(func.count()).select_from(Subject))
    students_active = await _scalar(
        db,
        select(func.count(func.distinct(RequestEvent.subject_id))).where(
            RequestEvent.created_at >= since
        ),
    )
    requests = await _scalar(
        db, select(func.count()).select_from(RequestEvent).where(RequestEvent.created_at >= since)
    )
    failed = await _scalar(
        db,
        select(func.count())
        .select_from(RequestState)
        .where(RequestState.created_at >= since, RequestState.status != "COMPLETED"),
    )
    questions = await _scalar(
        db,
        select(func.count())
        .select_from(Message)
        .where(Message.created_at >= since, Message.role == "STUDENT"),
    )
    media = await _scalar(
        db, select(func.count()).select_from(MediaObject).where(MediaObject.created_at >= since)
    )
    mocks = await _scalar(
        db,
        select(func.count())
        .select_from(Assessment)
        .where(Assessment.created_at >= since, Assessment.kind == "MOCK_TEST"),
    )
    model_calls = await _scalar(
        db, select(func.count()).select_from(UsageLedger).where(UsageLedger.created_at >= since)
    )
    cost = await _scalar(
        db,
        select(func.coalesce(func.sum(UsageLedger.estimated_cost_micros), 0)).where(
            UsageLedger.created_at >= since
        ),
    )
    verifier_calls = await _scalar(
        db,
        select(func.count())
        .select_from(UsageLedger)
        .where(UsageLedger.created_at >= since, UsageLedger.model_alias.like("VERIFIER%")),
    )
    # A blocked request is one the budget gate refused. It is a success of the
    # cost controls, so it is shown next to spend rather than under failures.
    quota_blocked = await _scalar(
        db,
        select(func.count())
        .select_from(RequestState)
        .where(
            RequestState.created_at >= since,
            RequestState.error_code.in_(("ENTITLEMENT_INACTIVE", "RATE_LIMITED")),
        ),
    )
    jobs_failed = await _scalar(
        db,
        select(func.count())
        .select_from(Job)
        .where(Job.updated_at >= since, Job.state.in_(("FAILED", "FAILED_PERMANENT"))),
    )
    # Reusable cache entries, not hits: nothing records a hit, and reporting a
    # number the system never measured would be worse than reporting the one it
    # does. One row per (subject, content, parser version, page).
    cache_entries = await _scalar(
        db,
        select(func.count())
        .select_from(MediaExtraction)
        .where(MediaExtraction.created_at >= since),
    )
    local_extractions = await _scalar(
        db,
        select(func.count())
        .select_from(MediaExtraction)
        .where(
            MediaExtraction.created_at >= since,
            MediaExtraction.method.in_(("DIGITAL_TEXT", "LOCAL_OCR")),
        ),
    )
    vision = await _scalar(
        db,
        select(func.count())
        .select_from(MediaExtraction)
        .where(MediaExtraction.created_at >= since, MediaExtraction.method == "VISION"),
    )
    sources = await _scalar(
        db,
        select(func.count())
        .select_from(KnowledgeSource)
        .where(KnowledgeSource.deleted_at.is_(None)),
    )
    chunks = await _scalar(db, select(func.count()).select_from(DocumentChunk))

    tokens = (
        await db.execute(
            select(
                func.coalesce(func.sum(UsageLedger.input_tokens), 0),
                func.coalesce(func.sum(UsageLedger.cached_tokens), 0),
            ).where(UsageLedger.created_at >= since)
        )
    ).one()

    # End-to-end request latency, measured from data the system already wrote:
    # the inbound event row and the terminal state row bracket the whole request,
    # including queueing and extraction, not just the provider call. A provider
    # -only number would flatter the service by hiding everything around it.
    #
    # Percentiles rather than a mean: one 40-second OCR request moves a mean and
    # tells an operator nothing about what most students experienced.
    elapsed_ms = func.extract("epoch", RequestState.created_at - RequestEvent.created_at) * 1000
    latency = (
        await db.execute(
            select(
                func.percentile_cont(0.5).within_group(elapsed_ms.asc()),
                func.percentile_cont(0.95).within_group(elapsed_ms.asc()),
            )
            .select_from(RequestState)
            .join(RequestEvent, RequestEvent.id == RequestState.request_event_id)
            .where(RequestState.created_at >= since)
        )
    ).one()

    by_alias = (
        await db.execute(
            select(
                UsageLedger.model_alias,
                UsageLedger.provider,
                func.count().label("calls"),
                func.coalesce(func.sum(UsageLedger.estimated_cost_micros), 0).label("micros"),
                func.coalesce(func.sum(UsageLedger.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLedger.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLedger.cached_tokens), 0).label("cached_tokens"),
            )
            .where(UsageLedger.created_at >= since)
            .group_by(UsageLedger.model_alias, UsageLedger.provider)
            .order_by(func.sum(UsageLedger.estimated_cost_micros).desc())
        )
    ).all()

    failures = (
        await db.execute(
            select(RequestState.error_code, func.count().label("count"))
            .where(RequestState.created_at >= since, RequestState.status != "COMPLETED")
            .group_by(RequestState.error_code)
            .order_by(func.count().desc())
        )
    ).all()

    return DashboardResponse(
        window_days=window_days,
        generated_at=datetime.now().astimezone(),
        counts=DashboardCounts(
            students_total=students_total,
            students_active_in_window=students_active,
            requests=requests,
            requests_failed=failed,
            questions_answered=questions,
            media_objects=media,
            mock_tests=mocks,
            model_calls=model_calls,
            cost_micros=cost,
            verifier_calls=verifier_calls,
            quota_blocked=quota_blocked,
            jobs_failed=jobs_failed,
            extraction_cache_entries=cache_entries,
            local_extractions=local_extractions,
            vision_escalations=vision,
            rag_sources=sources,
            rag_chunks=chunks,
            input_tokens=int(tokens[0] or 0),
            cached_input_tokens=int(tokens[1] or 0),
            latency_p50_ms=None if latency[0] is None else int(latency[0]),
            latency_p95_ms=None if latency[1] is None else int(latency[1]),
        ),
        cost_by_alias=[
            {
                "model_alias": row.model_alias,
                "provider": row.provider,
                "calls": row.calls,
                "cost_micros": int(row.micros),
                "input_tokens": int(row.input_tokens),
                "output_tokens": int(row.output_tokens),
                "cached_tokens": int(row.cached_tokens),
            }
            for row in by_alias
        ],
        failures_by_code=[
            {"error_code": row.error_code or "UNKNOWN", "count": row.count} for row in failures
        ],
    )


CostGrouping = Literal[
    "model",
    "provider",
    "capability",
    "student",
    "tutor",
    "media",
    "verification",
    "day",
]

# Aliases whose spend is media work rather than tutoring text. Grouping on the
# alias is what the ledger can actually prove: the row records which model was
# called, and a vision call is a vision call whatever asked for it.
MEDIA_ALIASES = ("VISION", "TRANSCRIBE")


class CostBucket(BaseModel):
    key: str
    label: str
    calls: int
    cost_micros: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int


class CostResponse(BaseModel):
    window_days: int
    group_by: CostGrouping
    total_cost_micros: int
    total_calls: int
    cached_tokens: int
    buckets: list[CostBucket]


@router.get("/costs", response_model=CostResponse)
async def costs(
    db: DbSession,
    window_days: WindowDays = 30,
    group_by: CostGrouping = "model",
    _: AdminActor = Depends(require(Permission.COST_READ)),
) -> CostResponse:
    """Spend, grouped. `group_by` is a closed literal, never interpolated SQL."""
    since = admin_repo.window_start(window_days)

    # A closed literal chooses a column expression; nothing here is interpolated,
    # so no filter value ever reaches SQL as text.
    key_column: Any
    needs_tutor_join = False
    if group_by == "model":
        key_column = UsageLedger.model_alias
    elif group_by == "provider":
        key_column = UsageLedger.provider
    elif group_by == "capability":
        # Null on every row written before the column existed, and on any call a
        # capability did not originate. Named as such rather than folded into a
        # capability that did not spend it.
        key_column = func.coalesce(UsageLedger.capability, "unattributed")
    elif group_by == "student":
        key_column = func.coalesce(cast(UsageLedger.subject_id, String), "unattributed")
    elif group_by == "tutor":
        # Attribution follows the *current* active assignment, because that is
        # the only tutor link the ledger row can be resolved through. Reassigning
        # a student therefore moves their historical spend - which is why the
        # response labels this grouping rather than the UI inventing a caveat.
        key_column = func.coalesce(Tutor.display_name, "unassigned")
        needs_tutor_join = True
    elif group_by == "media":
        key_column = case(
            (UsageLedger.model_alias.in_(MEDIA_ALIASES), UsageLedger.model_alias),
            else_="text",
        )
    elif group_by == "verification":
        key_column = case(
            (UsageLedger.model_alias.like("VERIFIER%"), "verification"),
            else_="answering",
        )
    else:
        key_column = func.to_char(UsageLedger.created_at, "YYYY-MM-DD")

    statement = select(
        key_column.label("key"),
        func.count().label("calls"),
        func.coalesce(func.sum(UsageLedger.estimated_cost_micros), 0).label("micros"),
        func.coalesce(func.sum(UsageLedger.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(UsageLedger.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(UsageLedger.cached_tokens), 0).label("cached_tokens"),
    ).select_from(UsageLedger)

    if needs_tutor_join:
        statement = statement.outerjoin(
            TutorAssignment,
            and_(
                TutorAssignment.subject_id == UsageLedger.subject_id,
                TutorAssignment.is_active.is_(True),
            ),
        ).outerjoin(Tutor, Tutor.id == TutorAssignment.tutor_id)

    rows = (
        await db.execute(
            statement.where(UsageLedger.created_at >= since)
            .group_by(key_column)
            .order_by(func.sum(UsageLedger.estimated_cost_micros).desc())
            .limit(admin_repo.MAX_PAGE_SIZE)
        )
    ).all()

    buckets = [
        CostBucket(
            key=str(row.key),
            label=str(row.key),
            calls=row.calls,
            cost_micros=int(row.micros),
            input_tokens=int(row.input_tokens),
            output_tokens=int(row.output_tokens),
            cached_tokens=int(row.cached_tokens),
        )
        for row in rows
    ]
    return CostResponse(
        window_days=window_days,
        group_by=group_by,
        total_cost_micros=sum(b.cost_micros for b in buckets),
        total_calls=sum(b.calls for b in buckets),
        cached_tokens=sum(b.cached_tokens for b in buckets),
        buckets=buckets,
    )


class AuditEntry(BaseModel):
    id: str
    actor_type: str
    actor_id: str | None
    actor_email: str | None
    action: str
    target_type: str | None
    target_id: str | None
    reason: str | None
    high_risk: bool
    detail: dict[str, Any]
    created_at: datetime


class AuditPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[AuditEntry]


@router.get("/audit", response_model=AuditPage)
async def audit_log(
    db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=admin_repo.DEFAULT_PAGE_SIZE, ge=1, le=admin_repo.MAX_PAGE_SIZE),
    action: str | None = Query(default=None, max_length=128),
    actor_id: str | None = Query(default=None, max_length=128),
    target_id: str | None = Query(default=None, max_length=128),
    high_risk_only: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
    _: AdminActor = Depends(require(Permission.AUDIT_READ)),
) -> AuditPage:
    """Filtered audit history. Every filter is a bound parameter."""
    conditions = []
    if action:
        conditions.append(AuditEvent.action == action)
    if actor_id:
        conditions.append(AuditEvent.actor_id == actor_id)
    if target_id:
        conditions.append(AuditEvent.target_id == target_id)
    if since:
        conditions.append(AuditEvent.created_at >= since)
    if until:
        conditions.append(AuditEvent.created_at <= until)
    if high_risk_only:
        conditions.append(AuditEvent.detail_json["high_risk"].astext == "true")

    base = select(AuditEvent).where(and_(*conditions)) if conditions else select(AuditEvent)
    total = await admin_repo.count_rows(db, base)

    rows = (
        await db.execute(
            admin_repo.paginate(
                base.order_by(AuditEvent.created_at.desc()), page=page, page_size=page_size
            )
        )
    ).scalars()
    events = list(rows)

    # One extra query resolves actor ids to emails; a join would repeat the admin
    # row for every event and the list is short.
    actor_ids = {e.actor_id for e in events if e.actor_id}
    emails: dict[str, str] = {}
    if actor_ids:
        found = await db.execute(
            select(AdminUser.id, AdminUser.email).where(AdminUser.id.in_(actor_ids))
        )
        emails = {str(row.id): row.email for row in found}

    return AuditPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[
            AuditEntry(
                id=str(event.id),
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                actor_email=emails.get(event.actor_id or ""),
                action=event.action,
                target_type=event.target_type,
                target_id=event.target_id,
                reason=(
                    str(event.detail_json["reason"]) if event.detail_json.get("reason") else None
                ),
                high_risk=bool(event.detail_json.get("high_risk")),
                detail=dict(event.detail_json),
                created_at=event.created_at,
            )
            for event in events
        ],
    )


class HealthSummary(BaseModel):
    conversations_open: int
    jobs_pending: int
    jobs_failed: int
    attempts_awaiting_grade: int


@router.get("/health-summary", response_model=HealthSummary)
async def health_summary(
    db: DbSession,
    _: AdminActor = Depends(require(Permission.DASHBOARD_READ)),
) -> HealthSummary:
    return HealthSummary(
        conversations_open=await _scalar(
            db,
            select(func.count()).select_from(Conversation).where(Conversation.status == "OPEN"),
        ),
        jobs_pending=await _scalar(
            db,
            select(func.count()).select_from(Job).where(Job.state.in_(("PENDING", "RUNNING"))),
        ),
        jobs_failed=await _scalar(
            db,
            select(func.count())
            .select_from(Job)
            .where(Job.state.in_(("FAILED", "FAILED_PERMANENT"))),
        ),
        attempts_awaiting_grade=await _scalar(
            db,
            select(func.count())
            .select_from(AssessmentAttempt)
            .where(AssessmentAttempt.state == "SUBMITTED"),
        ),
    )


__all__ = ["router"]
