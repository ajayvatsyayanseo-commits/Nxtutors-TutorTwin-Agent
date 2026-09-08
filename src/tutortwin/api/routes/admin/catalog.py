"""Plans, model routing, prompt versions and kill switches.

**No secret value is ever returned.** The model catalog holds an alias, a vendor
model id and a price; API keys live in the environment and have no row here, so
there is nothing for this endpoint to leak. `provider_key_configured` reports
whether a key is present as a boolean, which is the only fact an operator needs
and the only one that is safe to send.

**Plan policies are versioned, not edited.** A limit change creates a new
`(plan_code, version)` row, so a request refused last Tuesday can still be
explained by the policy that refused it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from tutortwin.api.admin_deps import DbSession, require
from tutortwin.config import MODEL_ALIASES, get_settings
from tutortwin.db.admin_models import PromptVersionRow
from tutortwin.db.models import FeatureFlag, ModelCatalog, PlanPolicy
from tutortwin.domain.admin import (
    KILL_SWITCH_KEYS,
    KILL_SWITCHES,
    AdminActor,
    HighRiskAction,
    HighRiskRequest,
    Permission,
)
from tutortwin.domain.errors import ErrorCode, TutorTwinError
from tutortwin.observability.logging import get_logger
from tutortwin.repositories import admin as admin_repo

router = APIRouter()
logger = get_logger(__name__)


# --- plans --------------------------------------------------------------------


class PlanView(BaseModel):
    plan_code: str
    version: int
    allows_paid_ai: bool
    features: dict[str, Any]
    limits: dict[str, Any]
    created_at: datetime


@router.get("/plans", response_model=list[PlanView])
async def list_plans(
    db: DbSession,
    include_history: bool = False,
    _: AdminActor = Depends(require(Permission.PLAN_READ)),
) -> list[PlanView]:
    rows = list(
        (
            await db.execute(
                select(PlanPolicy).order_by(PlanPolicy.plan_code, PlanPolicy.version.desc())
            )
        ).scalars()
    )
    if not include_history:
        latest: dict[str, PlanPolicy] = {}
        for row in rows:
            latest.setdefault(row.plan_code, row)
        rows = list(latest.values())

    return [
        PlanView(
            plan_code=row.plan_code,
            version=row.version,
            allows_paid_ai=row.allows_paid_ai,
            features=dict(row.features_json),
            limits=dict(row.limits_json),
            created_at=row.created_at,
        )
        for row in rows
    ]


class PlanUpdate(HighRiskRequest):
    plan_code: str = Field(min_length=1, max_length=64)
    allows_paid_ai: bool
    features: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)


@router.post("/plans", response_model=PlanView, status_code=201)
async def upsert_plan(
    payload: PlanUpdate,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.PLAN_WRITE)),
) -> PlanView:
    """Publish a new version of a plan policy. Never edits an existing row."""
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")

    current = (
        await db.execute(
            select(PlanPolicy)
            .where(PlanPolicy.plan_code == payload.plan_code)
            .order_by(PlanPolicy.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    row = PlanPolicy(
        plan_code=payload.plan_code,
        version=(current.version + 1) if current else 1,
        allows_paid_ai=payload.allows_paid_ai,
        features_json=payload.features,
        limits_json=payload.limits,
    )
    db.add(row)

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.PLAN_POLICY_CHANGE,
        target_type="plan",
        target_id=payload.plan_code,
        reason=payload.reason,
        before=(
            {
                "version": current.version,
                "allows_paid_ai": current.allows_paid_ai,
                "limits": dict(current.limits_json),
            }
            if current
            else {}
        ),
        after={
            "version": row.version,
            "allows_paid_ai": row.allows_paid_ai,
            "limits": payload.limits,
        },
    )
    await db.flush()
    view = PlanView(
        plan_code=row.plan_code,
        version=row.version,
        allows_paid_ai=row.allows_paid_ai,
        features=dict(row.features_json),
        limits=dict(row.limits_json),
        created_at=row.created_at,
    )
    await db.commit()
    return view


# --- models and routing -------------------------------------------------------


class ModelRouteView(BaseModel):
    id: str
    model_alias: str
    provider: str
    model_id: str
    is_active: bool
    input_cost_micros_per_1k: int
    output_cost_micros_per_1k: int
    rate_version: str
    created_at: datetime


class ModelCatalogResponse(BaseModel):
    aliases: list[str]
    routes: list[ModelRouteView]
    provider_key_configured: dict[str, bool]
    """Whether a credential exists, never what it is."""


@router.get("/models", response_model=ModelCatalogResponse)
async def list_models(
    db: DbSession,
    _: AdminActor = Depends(require(Permission.MODEL_READ)),
) -> ModelCatalogResponse:
    rows = list(
        (
            await db.execute(
                select(ModelCatalog).order_by(ModelCatalog.model_alias, ModelCatalog.provider)
            )
        ).scalars()
    )
    settings = get_settings()
    return ModelCatalogResponse(
        aliases=list(MODEL_ALIASES),
        routes=[
            ModelRouteView(
                id=str(row.id),
                model_alias=row.model_alias,
                provider=row.provider,
                model_id=row.model_id,
                is_active=row.is_active,
                input_cost_micros_per_1k=row.input_cost_micros_per_1k,
                output_cost_micros_per_1k=row.output_cost_micros_per_1k,
                rate_version=row.rate_version,
                created_at=row.created_at,
            )
            for row in rows
        ],
        provider_key_configured={
            "openai": settings.openai_api_key is not None,
            "anthropic": settings.anthropic_api_key is not None,
        },
    )


class ModelRouteUpsert(HighRiskRequest):
    model_alias: str = Field(min_length=1, max_length=64)
    provider: str = Field(pattern="^(OPENAI|ANTHROPIC|FAKE)$")
    model_id: str = Field(min_length=1, max_length=128)
    is_active: bool = True
    input_cost_micros_per_1k: int = Field(ge=0, le=10_000_000)
    output_cost_micros_per_1k: int = Field(ge=0, le=10_000_000)
    rate_version: str = Field(min_length=1, max_length=64)


@router.post("/models", response_model=ModelRouteView, status_code=201)
async def upsert_model_route(
    payload: ModelRouteUpsert,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.MODEL_WRITE)),
) -> ModelRouteView:
    """Add or update a capability route. High-risk: this is where money moves."""
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")
    if payload.model_alias not in MODEL_ALIASES:
        raise TutorTwinError(
            ErrorCode.VALIDATION_FAILED,
            f"Unknown alias. Known aliases: {', '.join(MODEL_ALIASES)}.",
        )

    existing = (
        await db.execute(
            select(ModelCatalog).where(
                ModelCatalog.model_alias == payload.model_alias,
                ModelCatalog.provider == payload.provider,
                ModelCatalog.model_id == payload.model_id,
            )
        )
    ).scalar_one_or_none()

    before: dict[str, Any] = {}
    if existing is None:
        row = ModelCatalog(
            model_alias=payload.model_alias,
            provider=payload.provider,
            model_id=payload.model_id,
            is_active=payload.is_active,
            input_cost_micros_per_1k=payload.input_cost_micros_per_1k,
            output_cost_micros_per_1k=payload.output_cost_micros_per_1k,
            rate_version=payload.rate_version,
        )
        db.add(row)
        await db.flush()
    else:
        row = existing
        before = {
            "is_active": row.is_active,
            "input_cost_micros_per_1k": row.input_cost_micros_per_1k,
            "output_cost_micros_per_1k": row.output_cost_micros_per_1k,
            "rate_version": row.rate_version,
        }
        row.is_active = payload.is_active
        row.input_cost_micros_per_1k = payload.input_cost_micros_per_1k
        row.output_cost_micros_per_1k = payload.output_cost_micros_per_1k
        row.rate_version = payload.rate_version

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.MODEL_ROUTE_CHANGE,
        target_type="model_route",
        target_id=f"{payload.model_alias}:{payload.provider}:{payload.model_id}",
        reason=payload.reason,
        before=before,
        after={
            "is_active": payload.is_active,
            "input_cost_micros_per_1k": payload.input_cost_micros_per_1k,
            "output_cost_micros_per_1k": payload.output_cost_micros_per_1k,
            "rate_version": payload.rate_version,
        },
    )
    await db.flush()
    view = ModelRouteView(
        id=str(row.id),
        model_alias=row.model_alias,
        provider=row.provider,
        model_id=row.model_id,
        is_active=row.is_active,
        input_cost_micros_per_1k=row.input_cost_micros_per_1k,
        output_cost_micros_per_1k=row.output_cost_micros_per_1k,
        rate_version=row.rate_version,
        created_at=row.created_at,
    )
    await db.commit()
    return view


# --- prompt versions ----------------------------------------------------------


class PromptVersionView(BaseModel):
    id: str
    block_key: str
    version: int
    status: str
    body: str
    reason: str | None
    author_admin_id: str | None
    activated_at: datetime | None
    created_at: datetime


@router.get("/prompts", response_model=list[PromptVersionView])
async def list_prompts(
    db: DbSession,
    block_key: str | None = Query(default=None, max_length=64),
    _: AdminActor = Depends(require(Permission.PROMPT_READ)),
) -> list[PromptVersionView]:
    base = select(PromptVersionRow)
    if block_key:
        base = base.where(PromptVersionRow.block_key == block_key)
    rows = (
        await db.execute(base.order_by(PromptVersionRow.block_key, PromptVersionRow.version.desc()))
    ).scalars()
    return [
        PromptVersionView(
            id=str(row.id),
            block_key=row.block_key,
            version=row.version,
            status=row.status,
            body=row.body,
            reason=row.reason,
            author_admin_id=str(row.author_admin_id) if row.author_admin_id else None,
            activated_at=row.activated_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


class PromptDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_key: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=8000)
    reason: str = Field(min_length=1, max_length=500)


@router.post("/prompts", response_model=PromptVersionView, status_code=201)
async def create_prompt_draft(
    payload: PromptDraft,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.PROMPT_WRITE)),
) -> PromptVersionView:
    version = await admin_repo.next_prompt_version(db, payload.block_key)
    row = PromptVersionRow(
        block_key=payload.block_key,
        version=version,
        body=payload.body,
        status="DRAFT",
        author_admin_id=actor.admin_id,
        reason=payload.reason,
    )
    db.add(row)
    await db.flush()

    admin_repo.record_audit(
        db,
        actor=actor,
        action="PROMPT_DRAFT_CREATED",
        target_type="prompt",
        target_id=payload.block_key,
        reason=payload.reason,
        detail={"version": version},
    )
    view = PromptVersionView(
        id=str(row.id),
        block_key=row.block_key,
        version=row.version,
        status=row.status,
        body=row.body,
        reason=row.reason,
        author_admin_id=str(row.author_admin_id) if row.author_admin_id else None,
        activated_at=None,
        created_at=row.created_at,
    )
    await db.commit()
    return view


@router.post("/prompts/{version_id}/activate", status_code=204)
async def activate_prompt(
    version_id: str,
    payload: HighRiskRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.PROMPT_WRITE)),
) -> None:
    """Activate a version. Rollback is the same call against an older version."""
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")

    previous = (
        await db.execute(
            select(PromptVersionRow.version, PromptVersionRow.block_key).where(
                PromptVersionRow.status == "ACTIVE"
            )
        )
    ).all()
    target = await admin_repo.activate_prompt_version(db, version_id=version_id)

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.PROMPT_ACTIVATION,
        target_type="prompt",
        target_id=target.block_key,
        reason=payload.reason,
        before={"active_versions": [{"block_key": key, "version": ver} for ver, key in previous]},
        after={"version": target.version},
    )
    await db.commit()


# --- feature flags / kill switches -------------------------------------------


class FlagView(BaseModel):
    key: str
    enabled: bool
    description: str | None
    is_kill_switch: bool
    updated_at: datetime


@router.get("/flags", response_model=list[FlagView])
async def list_flags(
    db: DbSession,
    _: AdminActor = Depends(require(Permission.FLAG_READ)),
) -> list[FlagView]:
    """Every known switch, including ones with no row yet.

    A switch that is missing from the list because nobody has toggled it yet
    looks like a switch that does not exist, which is the opposite of what a kill
    switch is for.
    """
    rows = {row.key: row for row in (await db.execute(select(FeatureFlag))).scalars()}
    now = datetime.now().astimezone()

    views = [
        FlagView(
            key=key,
            enabled=rows[key].enabled if key in rows else True,
            description=rows[key].description if key in rows else description,
            is_kill_switch=True,
            updated_at=rows[key].updated_at if key in rows else now,
        )
        for key, description in KILL_SWITCHES
    ]
    views += [
        FlagView(
            key=row.key,
            enabled=row.enabled,
            description=row.description,
            is_kill_switch=False,
            updated_at=row.updated_at,
        )
        for row in rows.values()
        if row.key not in KILL_SWITCH_KEYS
    ]
    return views


class FlagUpdate(HighRiskRequest):
    enabled: bool


@router.post("/flags/{key}", response_model=FlagView)
async def set_flag(
    key: str,
    payload: FlagUpdate,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.FLAG_WRITE)),
) -> FlagView:
    """Flip a kill switch. Turning something *off* is the safe direction."""
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")
    if key not in KILL_SWITCH_KEYS:
        raise TutorTwinError(
            ErrorCode.VALIDATION_FAILED,
            "Unknown switch. Inventing a key would create a control that nothing reads.",
        )

    row = (await db.execute(select(FeatureFlag).where(FeatureFlag.key == key))).scalar_one_or_none()
    description = next((d for k, d in KILL_SWITCHES if k == key), None)

    before = row.enabled if row else True
    if row is None:
        row = FeatureFlag(key=key, enabled=payload.enabled, description=description)
        db.add(row)
        await db.flush()
    else:
        row.enabled = payload.enabled

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.FEATURE_KILL_SWITCH,
        target_type="feature_flag",
        target_id=key,
        reason=payload.reason,
        before={"enabled": before},
        after={"enabled": payload.enabled},
    )
    await db.flush()
    await db.refresh(row)  # `updated_at` is server-updated; see jobs.retry_job
    view = FlagView(
        key=row.key,
        enabled=row.enabled,
        description=row.description,
        is_kill_switch=True,
        updated_at=row.updated_at,
    )
    await db.commit()
    logger.warning("feature_flag_changed", key=key, enabled=payload.enabled)
    return view


class QuotaResetRequest(HighRiskRequest):
    subject_id: str = Field(min_length=1, max_length=64)


@router.post("/quota/reset", status_code=204)
async def reset_quota(
    payload: QuotaResetRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.STUDENT_WRITE)),
) -> None:
    """Reset a student's daily counters.

    The counters are derived from `usage_ledger`, which is cost evidence and is
    never deleted. The reset is recorded as an audit event that the budgeter reads
    as a new starting point, so spend history stays intact and the override stays
    visible.
    """
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.QUOTA_RESET,
        target_type="student",
        target_id=payload.subject_id,
        reason=payload.reason,
        after={"reset_at": datetime.now().astimezone().isoformat()},
    )
    await db.commit()
    logger.warning("quota_reset", subject_id=payload.subject_id, actor=actor.admin_id)


@router.get("/flags/summary", response_model=dict[str, int])
async def flag_summary(
    db: DbSession,
    _: AdminActor = Depends(require(Permission.FLAG_READ)),
) -> dict[str, int]:
    disabled = (
        await db.execute(
            select(func.count()).select_from(FeatureFlag).where(FeatureFlag.enabled.is_(False))
        )
    ).scalar_one()
    return {"kill_switches": len(KILL_SWITCHES), "disabled": int(disabled)}


__all__ = ["router"]
