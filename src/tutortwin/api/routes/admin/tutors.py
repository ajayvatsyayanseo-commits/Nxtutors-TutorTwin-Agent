"""Tutors, persona versions and student assignment.

**A persona version is immutable once activated.** Editing a live persona would
make last week's answer unexplainable — the row that produced it would no longer
say what it said. Every change creates a new version, and activation is the only
mutation, which is also why activation is a high-risk action.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from tutortwin.api.admin_deps import DbSession, require
from tutortwin.api.routes.admin.students import like_pattern
from tutortwin.db.models import Subject, Tutor, TutorAssignment, TutorPersonaVersion
from tutortwin.domain.admin import (
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


class TutorSummary(BaseModel):
    id: str
    display_name: str
    status: str
    active_persona_version: int | None
    persona_versions: int
    assigned_students: int
    created_at: datetime


@router.get("/tutors", response_model=list[TutorSummary])
async def list_tutors(
    db: DbSession,
    q: str | None = Query(default=None, max_length=200),
    _: AdminActor = Depends(require(Permission.TUTOR_READ)),
) -> list[TutorSummary]:
    base = select(Tutor)
    if q:
        base = base.where(Tutor.display_name.ilike(like_pattern(q.strip()), escape="\\"))

    tutors = list((await db.execute(base.order_by(Tutor.display_name))).scalars())
    if not tutors:
        return []

    ids = [t.id for t in tutors]
    version_counts = {
        row[0]: int(row[1])
        for row in (
            await db.execute(
                select(TutorPersonaVersion.tutor_id, func.count())
                .where(TutorPersonaVersion.tutor_id.in_(ids))
                .group_by(TutorPersonaVersion.tutor_id)
            )
        ).all()
    }
    active_versions = {
        row[0]: int(row[1])
        for row in (
            await db.execute(
                select(TutorPersonaVersion.tutor_id, TutorPersonaVersion.version).where(
                    TutorPersonaVersion.tutor_id.in_(ids),
                    TutorPersonaVersion.is_active.is_(True),
                )
            )
        ).all()
    }
    assignment_counts = {
        row[0]: int(row[1])
        for row in (
            await db.execute(
                select(TutorAssignment.tutor_id, func.count())
                .where(TutorAssignment.tutor_id.in_(ids), TutorAssignment.is_active.is_(True))
                .group_by(TutorAssignment.tutor_id)
            )
        ).all()
    }

    return [
        TutorSummary(
            id=str(t.id),
            display_name=t.display_name,
            status=t.status,
            active_persona_version=active_versions.get(t.id),
            persona_versions=version_counts.get(t.id, 0),
            assigned_students=assignment_counts.get(t.id, 0),
            created_at=t.created_at,
        )
        for t in tutors
    ]


class CreateTutorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=256)


@router.post("/tutors", response_model=TutorSummary, status_code=201)
async def create_tutor(
    payload: CreateTutorRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.TUTOR_WRITE)),
) -> TutorSummary:
    tutor = Tutor(display_name=payload.display_name)
    db.add(tutor)
    await db.flush()

    admin_repo.record_audit(
        db,
        actor=actor,
        action="TUTOR_CREATED",
        target_type="tutor",
        target_id=str(tutor.id),
        detail={"display_name": tutor.display_name},
    )
    view = TutorSummary(
        id=str(tutor.id),
        display_name=tutor.display_name,
        status=tutor.status,
        active_persona_version=None,
        persona_versions=0,
        assigned_students=0,
        created_at=tutor.created_at,
    )
    await db.commit()
    return view


class PersonaVersionView(BaseModel):
    id: str
    version: int
    is_active: bool
    persona: dict[str, Any]
    created_at: datetime


class TutorDetail(BaseModel):
    tutor: TutorSummary
    personas: list[PersonaVersionView]
    students: list[dict[str, Any]]


@router.get("/tutors/{tutor_id}", response_model=TutorDetail)
async def tutor_detail(
    tutor_id: UUID,
    db: DbSession,
    _: AdminActor = Depends(require(Permission.TUTOR_READ)),
) -> TutorDetail:
    tutor = (await db.execute(select(Tutor).where(Tutor.id == tutor_id))).scalar_one_or_none()
    if tutor is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Tutor not found.")

    personas = list(
        (
            await db.execute(
                select(TutorPersonaVersion)
                .where(TutorPersonaVersion.tutor_id == tutor_id)
                .order_by(TutorPersonaVersion.version.desc())
            )
        ).scalars()
    )
    students = (
        await db.execute(
            select(Subject, TutorAssignment)
            .join(TutorAssignment, TutorAssignment.subject_id == Subject.id)
            .where(TutorAssignment.tutor_id == tutor_id)
            .order_by(TutorAssignment.created_at.desc())
            .limit(admin_repo.MAX_PAGE_SIZE)
        )
    ).all()

    active = next((p for p in personas if p.is_active), None)
    return TutorDetail(
        tutor=TutorSummary(
            id=str(tutor.id),
            display_name=tutor.display_name,
            status=tutor.status,
            active_persona_version=active.version if active else None,
            persona_versions=len(personas),
            assigned_students=sum(1 for _s, a in students if a.is_active),
            created_at=tutor.created_at,
        ),
        personas=[
            PersonaVersionView(
                id=str(p.id),
                version=p.version,
                is_active=p.is_active,
                persona=dict(p.persona_json),
                created_at=p.created_at,
            )
            for p in personas
        ],
        students=[
            {
                "subject_id": str(subject.id),
                "identity": subject.external_identity_value,
                "display_name": subject.display_name,
                "is_active": assignment.is_active,
                "assigned_at": assignment.created_at,
            }
            for subject, assignment in students
        ],
    )


class PersonaDraft(BaseModel):
    """The persona fields the control plane edits.

    Closed shape rather than free-form JSON: a persona is read into the system
    prompt, so an unbounded object is an unbounded prompt.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(max_length=120)
    avatar_url: str = Field(default="", max_length=512)
    subjects: list[str] = Field(default_factory=list, max_length=20)
    tone: str = Field(default="", max_length=200)
    pedagogy_mode: str = Field(default="GUIDED", max_length=32)
    language: str = Field(default="en", max_length=32)
    response_style: str = Field(default="", max_length=400)
    notification_preference: str = Field(default="none", max_length=32)
    """Placeholder until a delivery channel exists. Stored, never acted on."""

    signature_phrases: list[str] = Field(default_factory=list, max_length=10)


@router.post("/tutors/{tutor_id}/personas", response_model=PersonaVersionView, status_code=201)
async def create_persona_draft(
    tutor_id: UUID,
    payload: PersonaDraft,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.TUTOR_WRITE)),
) -> PersonaVersionView:
    """Create a draft. Drafts are inert until activated."""
    tutor = (await db.execute(select(Tutor).where(Tutor.id == tutor_id))).scalar_one_or_none()
    if tutor is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Tutor not found.")

    highest = (
        await db.execute(
            select(func.max(TutorPersonaVersion.version)).where(
                TutorPersonaVersion.tutor_id == tutor_id
            )
        )
    ).scalar_one_or_none()

    version = TutorPersonaVersion(
        tutor_id=tutor_id,
        version=int(highest or 0) + 1,
        is_active=False,
        persona_json=payload.model_dump(),
    )
    db.add(version)
    await db.flush()

    admin_repo.record_audit(
        db,
        actor=actor,
        action="PERSONA_DRAFT_CREATED",
        target_type="tutor",
        target_id=str(tutor_id),
        detail={"version": version.version},
    )
    view = PersonaVersionView(
        id=str(version.id),
        version=version.version,
        is_active=False,
        persona=dict(version.persona_json),
        created_at=version.created_at,
    )
    await db.commit()
    return view


@router.post("/tutors/{tutor_id}/personas/{version_id}/activate", status_code=204)
async def activate_persona(
    tutor_id: UUID,
    version_id: UUID,
    payload: HighRiskRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.PERSONA_ACTIVATE)),
) -> None:
    """Activate a persona version and deactivate the previous one.

    High-risk: this changes what every student assigned to the tutor hears on
    their next message.
    """
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")

    target = (
        await db.execute(
            select(TutorPersonaVersion).where(
                TutorPersonaVersion.id == version_id,
                TutorPersonaVersion.tutor_id == tutor_id,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Persona version not found.")

    previous = (
        await db.execute(
            select(TutorPersonaVersion).where(
                TutorPersonaVersion.tutor_id == tutor_id,
                TutorPersonaVersion.is_active.is_(True),
                TutorPersonaVersion.id != version_id,
            )
        )
    ).scalars()
    previous_versions = [p.version for p in previous]
    for row in await db.execute(
        select(TutorPersonaVersion).where(
            TutorPersonaVersion.tutor_id == tutor_id,
            TutorPersonaVersion.is_active.is_(True),
            TutorPersonaVersion.id != version_id,
        )
    ):
        row[0].is_active = False

    target.is_active = True

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.PERSONA_ACTIVATION,
        target_type="tutor",
        target_id=str(tutor_id),
        reason=payload.reason,
        before={"active_versions": previous_versions},
        after={"active_version": target.version},
    )
    await db.commit()


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: UUID
    reason: str = Field(min_length=1, max_length=500)


@router.post("/tutors/{tutor_id}/students", status_code=204)
async def assign_student(
    tutor_id: UUID,
    payload: AssignRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.TUTOR_WRITE)),
) -> None:
    """Assign a student to a tutor. Any previous active assignment is retired.

    One active tutor per student, enforced here rather than by a partial unique
    index, because the historical rows must remain readable.
    """
    tutor = (await db.execute(select(Tutor).where(Tutor.id == tutor_id))).scalar_one_or_none()
    if tutor is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Tutor not found.")
    subject = (
        await db.execute(select(Subject).where(Subject.id == payload.subject_id))
    ).scalar_one_or_none()
    if subject is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Student not found.")

    existing = (
        await db.execute(
            select(TutorAssignment).where(
                TutorAssignment.subject_id == payload.subject_id,
                TutorAssignment.is_active.is_(True),
            )
        )
    ).scalars()
    previous_tutor_ids = []
    for row in existing:
        previous_tutor_ids.append(str(row.tutor_id))
        row.is_active = False

    db.add(TutorAssignment(subject_id=payload.subject_id, tutor_id=tutor_id, is_active=True))

    admin_repo.record_audit(
        db,
        actor=actor,
        action="TUTOR_ASSIGNED",
        target_type="student",
        target_id=str(payload.subject_id),
        reason=payload.reason,
        detail={"tutor_id": str(tutor_id), "previous_tutor_ids": previous_tutor_ids},
    )
    await db.commit()


__all__ = ["router"]
