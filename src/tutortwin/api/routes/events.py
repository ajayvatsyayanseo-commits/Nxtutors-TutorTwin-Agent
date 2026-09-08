"""Event ingestion and conversation read."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from tutortwin.api.dependencies import Container, get_container
from tutortwin.db.engine import session_scope
from tutortwin.domain.errors import OwnershipError
from tutortwin.domain.events import EventResponse, NormalizedEvent, SubjectRef
from tutortwin.domain.models import SubjectStatus
from tutortwin.observability.logging import event_id_var
from tutortwin.repositories import conversations as repo

router = APIRouter(tags=["events"])


class MessageView(BaseModel):
    id: UUID
    role: str
    input_type: str
    text: str | None
    capability: str | None


class ConversationView(BaseModel):
    id: UUID
    subject_id: UUID
    status: str
    messages: list[MessageView]


@router.post("/events", response_model=EventResponse)
async def post_event(
    event: NormalizedEvent,
    container: Container = Depends(get_container),
    x_internal_key: str | None = Header(default=None, alias="x-internal-key"),
) -> EventResponse:
    container.internal_auth.verify_shared_secret(x_internal_key)
    token = event_id_var.set(event.event_id)
    try:
        # No session is opened here. The entry service owns its own transaction
        # boundaries because a provider call happens between them, and holding a
        # transaction across that network call would pin a Postgres connection
        # for the full model latency.
        return await container.entry_service.handle_event(event)
    finally:
        event_id_var.reset(token)


@router.get("/conversations/{conversation_id}", response_model=ConversationView)
async def get_conversation(
    conversation_id: UUID,
    subject_external_type: str,
    subject_external_id: str,
    container: Container = Depends(get_container),
    x_internal_key: str | None = Header(default=None, alias="x-internal-key"),
) -> ConversationView:
    """Ownership-checked read.

    The caller must state which subject is asking; the repository refuses to
    return another subject's conversation.
    """
    container.internal_auth.verify_shared_secret(x_internal_key)

    subject = await container.identity.resolve(
        SubjectRef(external_type=subject_external_type, external_id=subject_external_id)
    )
    if subject is None or subject.status is SubjectStatus.BLOCKED:
        raise OwnershipError()

    async with session_scope() as session:
        conversation = await repo.load_conversation(
            session, conversation_id=conversation_id, subject_id=subject.id
        )
        messages = await repo.list_messages(session, conversation_id=conversation.id)
        return ConversationView(
            id=conversation.id,
            subject_id=conversation.subject_id,
            status=conversation.status,
            messages=[
                MessageView(
                    id=m.id,
                    role=m.role,
                    input_type=m.input_type,
                    text=m.text,
                    capability=m.capability,
                )
                for m in messages
            ],
        )
