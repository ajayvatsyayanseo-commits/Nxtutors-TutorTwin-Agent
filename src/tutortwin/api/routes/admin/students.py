"""Students, conversations, documents and learning records.

**Search is a bound parameter, always.** Every filter below reaches SQL through
SQLAlchemy binds; nothing is formatted into a string. The `%` and `_` characters
in a `LIKE` pattern are escaped too — unescaped they are wildcards, so a search
for `_` would silently match every student rather than the ones containing an
underscore.

**Student content is shown, media bytes are not.** A support operator needs the
normalised text of a request to answer "why did this fail". They do not need the
student's photograph, so media appears as metadata and a blob key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from tutortwin.api.admin_deps import DbSession, require
from tutortwin.db.knowledge_models import (
    DocumentChunk,
    KnowledgeSource,
    RetrievalEventRow,
    StudentMemoryRow,
    TopicStat,
)
from tutortwin.db.learning_models import (
    Assessment,
    AssessmentAttempt,
    Flashcard,
    FlashcardDeck,
    LearningArtifact,
)
from tutortwin.db.models import (
    Conversation,
    Entitlement,
    MediaObject,
    Message,
    OutboundActionRow,
    RequestEvent,
    RequestState,
    Subject,
    Tutor,
    TutorAssignment,
    UsageLedger,
)
from tutortwin.domain.admin import (
    AdminActor,
    HighRiskAction,
    HighRiskRequest,
    Permission,
)
from tutortwin.domain.errors import ErrorCode, TutorTwinError
from tutortwin.observability.logging import get_logger
from tutortwin.repositories import admin as admin_repo
from tutortwin.services import subscriptions

router = APIRouter()
logger = get_logger(__name__)


_CONTROL_CHARS = frozenset(chr(code) for code in range(32)) | {chr(127)}


def like_pattern(term: str) -> str:
    """Escape LIKE wildcards so a search term matches itself and nothing else.

    Control characters are stripped first. A NUL byte is not merely useless in a
    search - PostgreSQL text cannot hold one, so passing it through turns a
    hostile query parameter into a driver error and a 500. Measured: without this
    line, `?q=%00truncated` crashed the endpoint.
    """
    cleaned = "".join(ch for ch in term if ch not in _CONTROL_CHARS)
    escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class StudentSummary(BaseModel):
    id: str
    external_identity_type: str
    external_identity_value: str
    display_name: str | None
    status: str
    plan_code: str | None
    entitlement_status: str | None
    tutor_id: str | None
    tutor_name: str | None
    created_at: datetime


class StudentPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[StudentSummary]


@router.get("/students", response_model=StudentPage)
async def list_students(
    db: DbSession,
    q: str | None = Query(default=None, max_length=200),
    plan_code: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=admin_repo.DEFAULT_PAGE_SIZE, ge=1, le=admin_repo.MAX_PAGE_SIZE),
    _: AdminActor = Depends(require(Permission.STUDENT_READ)),
) -> StudentPage:
    base = (
        select(Subject, Entitlement, Tutor)
        .outerjoin(
            Entitlement,
            (Entitlement.subject_id == Subject.id) & (Entitlement.status == "ACTIVE"),
        )
        .outerjoin(
            TutorAssignment,
            (TutorAssignment.subject_id == Subject.id) & TutorAssignment.is_active.is_(True),
        )
        .outerjoin(Tutor, Tutor.id == TutorAssignment.tutor_id)
    )

    if q:
        pattern = like_pattern(q.strip())
        base = base.where(
            or_(
                Subject.external_identity_value.ilike(pattern, escape="\\"),
                Subject.display_name.ilike(pattern, escape="\\"),
            )
        )
    if plan_code:
        base = base.where(Entitlement.plan_code == plan_code)
    if status:
        base = base.where(Subject.status == status)

    total = await admin_repo.count_rows(db, base)
    rows = (
        await db.execute(
            admin_repo.paginate(
                base.order_by(Subject.created_at.desc()), page=page, page_size=page_size
            )
        )
    ).all()

    return StudentPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[
            StudentSummary(
                id=str(subject.id),
                external_identity_type=subject.external_identity_type,
                external_identity_value=subject.external_identity_value,
                display_name=subject.display_name,
                status=subject.status,
                plan_code=entitlement.plan_code if entitlement else None,
                entitlement_status=entitlement.status if entitlement else None,
                tutor_id=str(tutor.id) if tutor else None,
                tutor_name=tutor.display_name if tutor else None,
                created_at=subject.created_at,
            )
            for subject, entitlement, tutor in rows
        ],
    )


class StudentDetail(BaseModel):
    student: StudentSummary
    entitlements: list[dict[str, Any]]
    usage: dict[str, int]
    conversations: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    assessments: list[dict[str, Any]]
    progress: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    decks: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]


async def _load_subject(db: DbSession, student_id: UUID) -> Subject:
    subject = (
        await db.execute(select(Subject).where(Subject.id == student_id))
    ).scalar_one_or_none()
    if subject is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Student not found.")
    return subject


@router.get("/students/{student_id}", response_model=StudentDetail)
async def student_detail(
    student_id: UUID,
    db: DbSession,
    _: AdminActor = Depends(require(Permission.STUDENT_READ)),
) -> StudentDetail:
    subject = await _load_subject(db, student_id)

    entitlements = (
        await db.execute(
            select(Entitlement)
            .where(Entitlement.subject_id == student_id)
            .order_by(Entitlement.fetched_at.desc())
            .limit(20)
        )
    ).scalars()

    assignment = (
        await db.execute(
            select(Tutor)
            .join(TutorAssignment, TutorAssignment.tutor_id == Tutor.id)
            .where(
                TutorAssignment.subject_id == student_id,
                TutorAssignment.is_active.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    usage_row = (
        await db.execute(
            select(
                func.count().label("calls"),
                func.coalesce(func.sum(UsageLedger.estimated_cost_micros), 0).label("micros"),
                func.coalesce(func.sum(UsageLedger.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLedger.output_tokens), 0).label("output_tokens"),
            ).where(UsageLedger.subject_id == student_id)
        )
    ).one()

    request_count = (
        await db.execute(
            select(func.count())
            .select_from(RequestEvent)
            .where(RequestEvent.subject_id == student_id)
        )
    ).scalar_one()

    conversations = (
        await db.execute(
            select(Conversation)
            .where(Conversation.subject_id == student_id)
            .order_by(Conversation.last_activity_at.desc())
            .limit(25)
        )
    ).scalars()

    documents = (
        await db.execute(
            select(KnowledgeSource)
            .where(KnowledgeSource.subject_id == student_id)
            .order_by(KnowledgeSource.created_at.desc())
            .limit(25)
        )
    ).scalars()

    assessments = (
        await db.execute(
            select(Assessment)
            .where(Assessment.subject_id == student_id)
            .order_by(Assessment.created_at.desc())
            .limit(25)
        )
    ).scalars()

    progress = (
        await db.execute(
            select(TopicStat)
            .where(TopicStat.subject_id == student_id)
            .order_by(TopicStat.last_seen_at.desc())
            .limit(50)
        )
    ).scalars()

    memories = (
        await db.execute(
            select(StudentMemoryRow)
            .where(
                StudentMemoryRow.subject_id == student_id,
                StudentMemoryRow.superseded_by.is_(None),
            )
            .order_by(StudentMemoryRow.updated_at.desc())
            .limit(50)
        )
    ).scalars()

    decks = (
        await db.execute(
            select(FlashcardDeck, func.count(Flashcard.id))
            .outerjoin(Flashcard, Flashcard.deck_id == FlashcardDeck.id)
            .where(FlashcardDeck.subject_id == student_id)
            .group_by(FlashcardDeck.id)
            .order_by(FlashcardDeck.created_at.desc())
            .limit(25)
        )
    ).all()

    artifacts = (
        await db.execute(
            select(LearningArtifact)
            .where(LearningArtifact.subject_id == student_id)
            .order_by(LearningArtifact.created_at.desc())
            .limit(25)
        )
    ).scalars()

    return StudentDetail(
        student=StudentSummary(
            id=str(subject.id),
            external_identity_type=subject.external_identity_type,
            external_identity_value=subject.external_identity_value,
            display_name=subject.display_name,
            status=subject.status,
            plan_code=None,
            entitlement_status=None,
            tutor_id=str(assignment.id) if assignment else None,
            tutor_name=assignment.display_name if assignment else None,
            created_at=subject.created_at,
        ),
        entitlements=[
            {
                "id": str(e.id),
                "plan_code": e.plan_code,
                "status": e.status,
                "source": e.source,
                "starts_at": e.starts_at,
                "ends_at": e.ends_at,
                "fetched_at": e.fetched_at,
            }
            for e in entitlements
        ],
        usage={
            "requests": int(request_count),
            "model_calls": int(usage_row.calls),
            "cost_micros": int(usage_row.micros),
            "input_tokens": int(usage_row.input_tokens),
            "output_tokens": int(usage_row.output_tokens),
        },
        conversations=[
            {
                "id": str(c.id),
                "status": c.status,
                "source": c.source,
                "created_at": c.created_at,
                "last_activity_at": c.last_activity_at,
            }
            for c in conversations
        ],
        documents=[
            {
                "id": str(d.id),
                "title": d.title,
                "kind": d.kind,
                "visibility": d.visibility,
                "status": d.status,
                "chunk_count": d.chunk_count,
                "content_sha256": d.content_sha256,
                "deleted_at": d.deleted_at,
                "created_at": d.created_at,
            }
            for d in documents
        ],
        assessments=[
            {
                "id": str(a.id),
                "kind": a.kind,
                "title": a.title,
                "topic": a.topic,
                "duration_minutes": a.duration_minutes,
                "total_marks": a.total_marks,
                "truncated_reason": a.truncated_reason,
                "created_at": a.created_at,
            }
            for a in assessments
        ],
        progress=[
            {
                "topic": p.topic,
                "attempts": p.attempts,
                "correct": p.correct,
                "hints_used": p.hints_used,
                "last_seen_at": p.last_seen_at,
            }
            for p in progress
        ],
        memories=[
            {
                "id": str(m.id),
                "kind": m.kind,
                "statement": m.statement,
                "confidence": m.confidence,
                "evidence": m.evidence,
                "observed_count": m.observed_count,
                "derived_by": m.derived_by,
                "updated_at": m.updated_at,
            }
            for m in memories
        ],
        decks=[
            {"id": str(deck.id), "name": deck.name, "topic": deck.topic, "cards": int(count)}
            for deck, count in decks
        ],
        artifacts=[
            {
                "id": str(a.id),
                "kind": a.kind,
                "artifact_format": a.artifact_format,
                "generated_by": a.generated_by,
                "sha256": a.sha256,
                "created_at": a.created_at,
            }
            for a in artifacts
        ],
    )


class EntitlementOverrideRequest(HighRiskRequest):
    plan_code: str = Field(min_length=1, max_length=64)
    status: str = Field(default="ACTIVE", pattern="^(ACTIVE|INACTIVE|SUSPENDED)$")
    ends_at: datetime | None = None


@router.post("/students/{student_id}/entitlement", status_code=204)
async def override_entitlement(
    student_id: UUID,
    payload: EntitlementOverrideRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.STUDENT_WRITE)),
) -> None:
    """Local entitlement override.

    Marked `source='admin_override'` so Phase 09 can tell an operator decision
    apart from what the website said. A refresh from the source of truth would
    otherwise silently undo this with no trace of either change.
    """
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")
    await _load_subject(db, student_id)

    current = (
        await db.execute(
            select(Entitlement)
            .where(Entitlement.subject_id == student_id, Entitlement.status == "ACTIVE")
            .limit(1)
        )
    ).scalar_one_or_none()

    before = {"plan_code": current.plan_code, "status": current.status} if current else {}
    if current is not None:
        current.status = "SUPERSEDED"

    db.add(
        Entitlement(
            subject_id=student_id,
            plan_code=payload.plan_code,
            status=payload.status,
            ends_at=payload.ends_at,
            source="admin_override",
            source_version=None,
            metadata_json={"overridden_by": actor.admin_id},
        )
    )

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.ENTITLEMENT_OVERRIDE,
        target_type="student",
        target_id=str(student_id),
        reason=payload.reason,
        before=before,
        after={"plan_code": payload.plan_code, "status": payload.status},
    )
    await db.commit()


class DeleteStudentDataRequest(HighRiskRequest):
    confirm_identity: str = Field(min_length=1, max_length=256)
    """The student's external identity, typed by the operator. A mis-clicked
    delete on the wrong row is unrecoverable, so the target must be named."""


@router.post("/students/{student_id}/delete-data", status_code=204)
async def delete_student_data(
    student_id: UUID,
    payload: DeleteStudentDataRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.STUDENT_WRITE)),
) -> None:
    """Erase a student's learning data. Identity row is retained for the audit.

    The audit event survives the deletion by design: a record that someone
    deleted a student's data is not itself student data.
    """
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")
    subject = await _load_subject(db, student_id)
    if payload.confirm_identity.strip() != subject.external_identity_value:
        raise TutorTwinError(
            ErrorCode.VALIDATION_FAILED,
            "Typed identity does not match the student being deleted.",
        )

    counts: dict[str, int] = {}
    from sqlalchemy import delete

    for label, table, column in (
        ("memories", StudentMemoryRow, StudentMemoryRow.subject_id),
        ("topic_stats", TopicStat, TopicStat.subject_id),
        ("documents", KnowledgeSource, KnowledgeSource.subject_id),
        ("assessments", Assessment, Assessment.subject_id),
        ("decks", FlashcardDeck, FlashcardDeck.subject_id),
        ("artifacts", LearningArtifact, LearningArtifact.subject_id),
        ("media", MediaObject, MediaObject.subject_id),
    ):
        result = await db.execute(delete(table).where(column == student_id))
        counts[label] = int(result.rowcount or 0)  # type: ignore[attr-defined]

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.STUDENT_DATA_DELETE,
        target_type="student",
        target_id=str(student_id),
        reason=payload.reason,
        after={"deleted": counts},
    )
    await db.commit()
    logger.warning("student_data_deleted", student_id=str(student_id), deleted=counts)


# --- conversations ------------------------------------------------------------


class ConversationSummary(BaseModel):
    id: str
    subject_id: str
    student_identity: str
    tutor_name: str | None
    status: str
    source: str
    message_count: int
    created_at: datetime
    last_activity_at: datetime


class ConversationPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ConversationSummary]


@router.get("/conversations", response_model=ConversationPage)
async def list_conversations(
    db: DbSession,
    student_id: UUID | None = None,
    status: str | None = Query(default=None, max_length=32),
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=admin_repo.DEFAULT_PAGE_SIZE, ge=1, le=admin_repo.MAX_PAGE_SIZE),
    _: AdminActor = Depends(require(Permission.CONVERSATION_READ)),
) -> ConversationPage:
    base = (
        select(Conversation, Subject, Tutor)
        .join(Subject, Subject.id == Conversation.subject_id)
        .outerjoin(Tutor, Tutor.id == Conversation.tutor_id)
    )
    if student_id:
        base = base.where(Conversation.subject_id == student_id)
    if status:
        base = base.where(Conversation.status == status)
    if since:
        base = base.where(Conversation.last_activity_at >= since)
    if until:
        base = base.where(Conversation.last_activity_at <= until)

    total = await admin_repo.count_rows(db, base)
    rows = (
        await db.execute(
            admin_repo.paginate(
                base.order_by(Conversation.last_activity_at.desc()),
                page=page,
                page_size=page_size,
            )
        )
    ).all()

    ids = [conversation.id for conversation, _s, _t in rows]
    counts: dict[UUID, int] = {}
    if ids:
        found = await db.execute(
            select(Message.conversation_id, func.count())
            .where(Message.conversation_id.in_(ids))
            .group_by(Message.conversation_id)
        )
        counts = {row[0]: int(row[1]) for row in found}

    return ConversationPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[
            ConversationSummary(
                id=str(conversation.id),
                subject_id=str(subject.id),
                student_identity=subject.external_identity_value,
                tutor_name=tutor.display_name if tutor else None,
                status=conversation.status,
                source=conversation.source,
                message_count=counts.get(conversation.id, 0),
                created_at=conversation.created_at,
                last_activity_at=conversation.last_activity_at,
            )
            for conversation, subject, tutor in rows
        ],
    )


class ConversationTimeline(BaseModel):
    conversation: ConversationSummary
    messages: list[dict[str, Any]]
    requests: list[dict[str, Any]]
    outbound: list[dict[str, Any]]
    model_calls: list[dict[str, Any]]
    retrievals: list[dict[str, Any]]
    cost_micros: int


@router.get("/conversations/{conversation_id}", response_model=ConversationTimeline)
async def conversation_detail(
    conversation_id: UUID,
    db: DbSession,
    _: AdminActor = Depends(require(Permission.CONVERSATION_READ)),
) -> ConversationTimeline:
    row = (
        await db.execute(
            select(Conversation, Subject, Tutor)
            .join(Subject, Subject.id == Conversation.subject_id)
            .outerjoin(Tutor, Tutor.id == Conversation.tutor_id)
            .where(Conversation.id == conversation_id)
        )
    ).one_or_none()
    if row is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Conversation not found.")
    conversation, subject, tutor = row

    messages = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
            .limit(500)
        )
    ).scalars()

    requests = (
        await db.execute(
            select(RequestEvent, RequestState)
            .outerjoin(RequestState, RequestState.request_event_id == RequestEvent.id)
            .where(RequestEvent.conversation_id == conversation_id)
            .order_by(RequestEvent.created_at)
            .limit(200)
        )
    ).all()

    outbound = (
        await db.execute(
            select(OutboundActionRow)
            .where(OutboundActionRow.conversation_id == conversation_id)
            .order_by(OutboundActionRow.created_at)
            .limit(200)
        )
    ).scalars()

    request_ids = [event.id for event, _ in requests]
    calls: list[Any] = []
    retrievals: list[Any] = []
    if request_ids:
        calls = list(
            (
                await db.execute(
                    select(UsageLedger)
                    .where(UsageLedger.request_event_id.in_(request_ids))
                    .order_by(UsageLedger.created_at)
                )
            ).scalars()
        )
        # What retrieval did for each turn, including the turns where it
        # deliberately did nothing: a skipped search with its reason explains a
        # thin answer, and an empty list here would look like a missing feature.
        retrievals = list(
            (
                await db.execute(
                    select(RetrievalEventRow)
                    .where(RetrievalEventRow.request_event_id.in_(request_ids))
                    .order_by(RetrievalEventRow.created_at)
                )
            ).scalars()
        )

    message_list = list(messages)
    return ConversationTimeline(
        conversation=ConversationSummary(
            id=str(conversation.id),
            subject_id=str(subject.id),
            student_identity=subject.external_identity_value,
            tutor_name=tutor.display_name if tutor else None,
            status=conversation.status,
            source=conversation.source,
            message_count=len(message_list),
            created_at=conversation.created_at,
            last_activity_at=conversation.last_activity_at,
        ),
        messages=[
            {
                "id": str(m.id),
                "role": m.role,
                "input_type": m.input_type,
                "text": m.text,
                "capability": m.capability,
                # Metadata only. The bytes stay in the BlobStore.
                "media": m.media_ref_json,
                "safety_flags": m.safety_flags_json,
                "created_at": m.created_at,
            }
            for m in message_list
        ],
        requests=[
            {
                "id": str(event.id),
                "request_id": event.request_id,
                "correlation_id": event.correlation_id,
                "message_type": event.message_type,
                "source": event.source,
                "status": state.status if state else None,
                "error_code": state.error_code if state else None,
                # Wall-clock for the whole turn, from the event arriving to its
                # terminal state. Null while a request is still in flight - zero
                # would read as "instant".
                "latency_ms": (
                    int((state.created_at - event.created_at).total_seconds() * 1000)
                    if state
                    else None
                ),
                "occurred_at": event.occurred_at,
                "created_at": event.created_at,
            }
            for event, state in requests
        ],
        outbound=[
            {
                "id": str(o.id),
                "action_type": o.action_type,
                "delivery_status": o.delivery_status,
                "payload": o.payload_json,
                "created_at": o.created_at,
            }
            for o in outbound
        ],
        model_calls=[
            {
                "id": str(c.id),
                "provider": c.provider,
                "model_alias": c.model_alias,
                "model_id": c.model_id,
                "capability": c.capability,
                # Verification is a routing fact, not a separate table: a call to
                # a VERIFIER_* alias *is* the second-model check.
                "is_verification": c.model_alias.startswith("VERIFIER"),
                "input_tokens": c.input_tokens,
                "output_tokens": c.output_tokens,
                "cached_tokens": c.cached_tokens,
                "cost_micros": c.estimated_cost_micros,
                "rate_version": c.rate_version,
                "created_at": c.created_at,
            }
            for c in calls
        ],
        retrievals=[
            {
                "id": str(r.id),
                "performed": r.performed,
                "skip_reason": r.skip_reason,
                "top_k": r.top_k,
                "returned": r.returned,
                "candidates_scanned": r.candidates_scanned,
                "embedding_calls": r.embedding_calls,
                "query_ms": r.query_ms,
                # Chunk **ids**. The text belongs to the document pages, and the
                # vectors are never rendered anywhere in the control plane.
                "chunk_ids": list(r.chunk_ids_json),
                "created_at": r.created_at,
            }
            for r in retrievals
        ],
        cost_micros=sum(int(c.estimated_cost_micros) for c in calls),
    )


# --- documents / RAG ----------------------------------------------------------


class DocumentSummary(BaseModel):
    id: str
    title: str
    kind: str
    visibility: str
    status: str
    owner_subject_id: str | None
    owner_identity: str | None
    tutor_id: str | None
    chunk_count: int
    content_sha256: str
    parser_version: str
    chunker_version: str
    embedding_model: str
    deleted_at: datetime | None
    created_at: datetime


class DocumentPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[DocumentSummary]


@router.get("/documents", response_model=DocumentPage)
async def list_documents(
    db: DbSession,
    q: str | None = Query(default=None, max_length=200),
    visibility: str | None = Query(default=None, max_length=24),
    status: str | None = Query(default=None, max_length=24),
    student_id: UUID | None = None,
    include_deleted: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=admin_repo.DEFAULT_PAGE_SIZE, ge=1, le=admin_repo.MAX_PAGE_SIZE),
    _: AdminActor = Depends(require(Permission.DOCUMENT_READ)),
) -> DocumentPage:
    base = select(KnowledgeSource, Subject).outerjoin(
        Subject, Subject.id == KnowledgeSource.subject_id
    )
    if q:
        base = base.where(KnowledgeSource.title.ilike(like_pattern(q.strip()), escape="\\"))
    if visibility:
        base = base.where(KnowledgeSource.visibility == visibility)
    if status:
        base = base.where(KnowledgeSource.status == status)
    if student_id:
        base = base.where(KnowledgeSource.subject_id == student_id)
    if not include_deleted:
        base = base.where(KnowledgeSource.deleted_at.is_(None))

    total = await admin_repo.count_rows(db, base)
    rows = (
        await db.execute(
            admin_repo.paginate(
                base.order_by(KnowledgeSource.created_at.desc()), page=page, page_size=page_size
            )
        )
    ).all()

    return DocumentPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[
            DocumentSummary(
                id=str(source.id),
                title=source.title,
                kind=source.kind,
                visibility=source.visibility,
                status=source.status,
                owner_subject_id=str(source.subject_id) if source.subject_id else None,
                owner_identity=subject.external_identity_value if subject else None,
                tutor_id=str(source.tutor_id) if source.tutor_id else None,
                chunk_count=source.chunk_count,
                content_sha256=source.content_sha256,
                parser_version=source.parser_version,
                chunker_version=source.chunker_version,
                embedding_model=source.embedding_model,
                deleted_at=source.deleted_at,
                created_at=source.created_at,
            )
            for source, subject in rows
        ],
    )


class DocumentDetail(BaseModel):
    document: DocumentSummary
    chunks: list[dict[str, Any]]


@router.get("/documents/{document_id}", response_model=DocumentDetail)
async def document_detail(
    document_id: UUID,
    db: DbSession,
    _: AdminActor = Depends(require(Permission.DOCUMENT_READ)),
) -> DocumentDetail:
    row = (
        await db.execute(
            select(KnowledgeSource, Subject)
            .outerjoin(Subject, Subject.id == KnowledgeSource.subject_id)
            .where(KnowledgeSource.id == document_id)
        )
    ).one_or_none()
    if row is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Document not found.")
    source, subject = row

    chunks = (
        await db.execute(
            select(DocumentChunk)
            .where(DocumentChunk.source_id == document_id)
            .order_by(DocumentChunk.ordinal)
            .limit(200)
        )
    ).scalars()

    return DocumentDetail(
        document=DocumentSummary(
            id=str(source.id),
            title=source.title,
            kind=source.kind,
            visibility=source.visibility,
            status=source.status,
            owner_subject_id=str(source.subject_id) if source.subject_id else None,
            owner_identity=subject.external_identity_value if subject else None,
            tutor_id=str(source.tutor_id) if source.tutor_id else None,
            chunk_count=source.chunk_count,
            content_sha256=source.content_sha256,
            parser_version=source.parser_version,
            chunker_version=source.chunker_version,
            embedding_model=source.embedding_model,
            deleted_at=source.deleted_at,
            created_at=source.created_at,
        ),
        chunks=[
            {
                "id": str(chunk.id),
                "ordinal": chunk.ordinal,
                "page_number": chunk.page_number,
                "section": chunk.section,
                "token_estimate": chunk.token_estimate,
                # Text, never the vector. A 1536-float array is unreadable in a
                # table and is not what an operator is diagnosing.
                "text_preview": chunk.text[:400],
                "has_embedding": chunk.embedding_json is not None,
            }
            for chunk in chunks
        ],
    )


@router.post("/documents/{document_id}/delete", status_code=204)
async def delete_document(
    document_id: UUID,
    payload: HighRiskRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.DOCUMENT_WRITE)),
) -> None:
    """Soft delete. Retrieval already excludes `deleted_at IS NOT NULL`."""
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")
    source = (
        await db.execute(select(KnowledgeSource).where(KnowledgeSource.id == document_id))
    ).scalar_one_or_none()
    if source is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Document not found.")

    source.deleted_at = datetime.now().astimezone()
    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.DOCUMENT_REPROCESS,
        target_type="document",
        target_id=str(document_id),
        reason=payload.reason,
        after={"deleted": True, "title": source.title},
    )
    await db.commit()


class ReprocessRequest(HighRiskRequest):
    pass


@router.post("/documents/{document_id}/reprocess", status_code=202)
async def reprocess_document(
    document_id: UUID,
    payload: ReprocessRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.DOCUMENT_WRITE)),
) -> dict[str, str]:
    """Mark a document for re-ingestion.

    High-risk because re-embedding a large document is a real bill: the chunk
    count is echoed back so the operator sees the size of what they triggered.
    """
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")
    source = (
        await db.execute(select(KnowledgeSource).where(KnowledgeSource.id == document_id))
    ).scalar_one_or_none()
    if source is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Document not found.")

    before = source.status
    source.status = "PENDING"
    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.DOCUMENT_REPROCESS,
        target_type="document",
        target_id=str(document_id),
        reason=payload.reason,
        before={"status": before},
        after={"status": "PENDING", "chunks_to_reembed": source.chunk_count},
    )
    await db.commit()
    return {"status": "PENDING", "chunks_to_reembed": str(source.chunk_count)}


# --- learning -----------------------------------------------------------------


class AssessmentSummary(BaseModel):
    id: str
    subject_id: str
    student_identity: str
    kind: str
    title: str
    topic: str | None
    duration_minutes: int
    total_marks: int
    truncated_reason: str | None
    attempts: int
    created_at: datetime


class AssessmentPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[AssessmentSummary]


@router.get("/learning/assessments", response_model=AssessmentPage)
async def list_assessments(
    db: DbSession,
    kind: str | None = Query(default=None, max_length=16),
    student_id: UUID | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=admin_repo.DEFAULT_PAGE_SIZE, ge=1, le=admin_repo.MAX_PAGE_SIZE),
    _: AdminActor = Depends(require(Permission.LEARNING_READ)),
) -> AssessmentPage:
    base = select(Assessment, Subject).join(Subject, Subject.id == Assessment.subject_id)
    if kind:
        base = base.where(Assessment.kind == kind)
    if student_id:
        base = base.where(Assessment.subject_id == student_id)

    total = await admin_repo.count_rows(db, base)
    rows = (
        await db.execute(
            admin_repo.paginate(
                base.order_by(Assessment.created_at.desc()), page=page, page_size=page_size
            )
        )
    ).all()

    ids = [assessment.id for assessment, _ in rows]
    attempt_counts: dict[UUID, int] = {}
    if ids:
        found = await db.execute(
            select(AssessmentAttempt.assessment_id, func.count())
            .where(AssessmentAttempt.assessment_id.in_(ids))
            .group_by(AssessmentAttempt.assessment_id)
        )
        attempt_counts = {row[0]: int(row[1]) for row in found}

    return AssessmentPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[
            AssessmentSummary(
                id=str(assessment.id),
                subject_id=str(subject.id),
                student_identity=subject.external_identity_value,
                kind=assessment.kind,
                title=assessment.title,
                topic=assessment.topic,
                duration_minutes=assessment.duration_minutes,
                total_marks=assessment.total_marks,
                truncated_reason=assessment.truncated_reason,
                attempts=attempt_counts.get(assessment.id, 0),
                created_at=assessment.created_at,
            )
            for assessment, subject in rows
        ],
    )


@router.get("/learning/assessments/{assessment_id}", response_model=dict[str, Any])
async def assessment_detail(
    assessment_id: UUID,
    db: DbSession,
    _: AdminActor = Depends(require(Permission.LEARNING_READ)),
) -> dict[str, Any]:
    """Full paper including the key.

    Unlike the student path, an operator inspecting a mock test needs the answer
    key: verifying that grading was correct is impossible without it. The
    difference is authorisation, which is why the key lives behind a permission
    rather than behind an omitted column.
    """
    from tutortwin.db.learning_models import AssessmentQuestion, AttemptResponse

    assessment = (
        await db.execute(select(Assessment).where(Assessment.id == assessment_id))
    ).scalar_one_or_none()
    if assessment is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Assessment not found.")

    questions = (
        await db.execute(
            select(AssessmentQuestion)
            .where(AssessmentQuestion.assessment_id == assessment_id)
            .order_by(AssessmentQuestion.number)
        )
    ).scalars()

    attempts = (
        await db.execute(
            select(AssessmentAttempt)
            .where(AssessmentAttempt.assessment_id == assessment_id)
            .order_by(AssessmentAttempt.started_at.desc())
        )
    ).scalars()
    attempt_rows = list(attempts)

    responses: dict[str, list[dict[str, Any]]] = {}
    if attempt_rows:
        found = (
            await db.execute(
                select(AttemptResponse)
                .where(AttemptResponse.attempt_id.in_([a.id for a in attempt_rows]))
                .order_by(AttemptResponse.number)
            )
        ).scalars()
        for response in found:
            responses.setdefault(str(response.attempt_id), []).append(
                {
                    "number": response.number,
                    "awarded_marks": response.awarded_marks,
                    "max_marks": response.max_marks,
                    "correct": response.correct,
                    "feedback": response.feedback,
                    "evidence": response.evidence,
                    "graded_by": response.graded_by,
                    "needs_review": response.needs_review,
                }
            )

    return {
        "assessment": {
            "id": str(assessment.id),
            "kind": assessment.kind,
            "title": assessment.title,
            "topic": assessment.topic,
            "duration_minutes": assessment.duration_minutes,
            "total_marks": assessment.total_marks,
            "blueprint": assessment.blueprint,
            "truncated_reason": assessment.truncated_reason,
            "created_at": assessment.created_at,
        },
        "questions": [
            {
                "number": q.number,
                "question_type": q.question_type,
                "prompt": q.prompt,
                "marks": q.marks,
                "options": q.options,
                "answer_key": q.answer_key,
            }
            for q in questions
        ],
        "attempts": [
            {
                "id": str(a.id),
                "state": a.state,
                "awarded_marks": a.awarded_marks,
                "total_marks": a.total_marks,
                "manual_review": a.manual_review,
                "model_calls": a.model_calls,
                "started_at": a.started_at,
                "submitted_at": a.submitted_at,
                "responses": responses.get(str(a.id), []),
            }
            for a in attempt_rows
        ],
    }


__all__ = ["like_pattern", "router"]


class GrantSubscriptionRequest(HighRiskRequest):
    """Give somebody a subscription without a payment.

    The WhatsApp number is the identity, not a student id, because the whole
    point of this endpoint is granting access to a person who has never used
    the product and therefore has no student row yet.
    """

    whatsapp_number: str = Field(min_length=6, max_length=32)
    student_name: str = Field(min_length=1, max_length=200)
    tutor_name: str = Field(default="TutorTwin", min_length=1, max_length=200)
    subject: str = Field(default="General", min_length=1, max_length=120)
    plan_code: str = Field(default="PRO", min_length=1, max_length=64)
    days: int = Field(default=30, ge=1, le=3660)
    notify: bool = True
    """Off only for a correction. A student who is not told they have a
    subscription behaves exactly like a student who does not have one."""


class GrantSubscriptionResponse(BaseModel):
    subject_id: UUID
    plan_code: str
    ends_at: datetime
    notified: bool


@router.post("/students/grant-subscription", response_model=GrantSubscriptionResponse)
async def grant_subscription(
    payload: GrantSubscriptionRequest,
    request: Request,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.STUDENT_WRITE)),
) -> GrantSubscriptionResponse:
    """Comp a subscription, and tell the student on WhatsApp.

    Routed through the same service as a paid activation - same tables, same
    supersede rule, same template - so a granted subscription behaves
    identically to a bought one everywhere downstream. Only `source` differs,
    which is what tells an operator later that no money was involved.
    """
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")

    result = await subscriptions.grant_manual_subscription(
        db,
        whatsapp_number=payload.whatsapp_number,
        student_name=payload.student_name.strip(),
        tutor_name=payload.tutor_name.strip(),
        subject_name=payload.subject.strip(),
        plan_code=payload.plan_code,
        days=payload.days,
        granted_by=actor.admin_id,
    )

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.ENTITLEMENT_OVERRIDE,
        target_type="student",
        target_id=str(result.subject_id),
        reason=payload.reason,
        before={},
        after={
            "plan_code": payload.plan_code,
            "days": payload.days,
            "whatsapp_number": result.whatsapp_number,
            "source": "admin_grant",
        },
    )
    await db.commit()

    # Notified after the commit. A WhatsApp send is a call to somebody else's
    # service, and a failure there must not roll back a grant the operator has
    # already been told succeeded.
    notified = False
    if payload.notify:
        from tutortwin.api.routes.public import notify_activation

        container = request.app.state.container
        await notify_activation(container, result)
        notified = container.whatsapp is not None

    assert result.subject_id is not None and result.ends_at is not None  # noqa: S101
    return GrantSubscriptionResponse(
        subject_id=result.subject_id,
        plan_code=result.plan_code or payload.plan_code,
        ends_at=result.ends_at,
        notified=notified,
    )
