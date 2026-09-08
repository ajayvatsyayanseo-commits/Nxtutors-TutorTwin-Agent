"""Persistence for subjects, conversations, messages and idempotency.

Ownership is enforced here, at the repository layer, so no caller can forget it:
`load_conversation` requires the owning subject id and treats a mismatch as
not-found rather than forbidden (existence is not leaked).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.models import (
    Conversation,
    IdempotencyKey,
    Message,
    OutboundActionRow,
    RequestEvent,
    RequestState,
    Subject,
    Tutor,
    TutorPersonaVersion,
)
from tutortwin.domain.errors import OwnershipError
from tutortwin.domain.events import MediaRef, NormalizedEvent, OutboundAction


async def upsert_subject(
    session: AsyncSession,
    *,
    subject_id: UUID,
    external_type: str,
    external_id: str,
    display_name: str | None,
) -> UUID:
    """Idempotent by (external_type, external_id)."""
    stmt = (
        pg_insert(Subject)
        .values(
            id=subject_id,
            external_identity_type=external_type,
            external_identity_value=external_id,
            display_name=display_name,
        )
        .on_conflict_do_update(
            constraint="uq_subject_external",
            set_={"display_name": display_name},
        )
        .returning(Subject.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def upsert_tutor(
    session: AsyncSession, *, tutor_id: UUID, display_name: str, persona: dict[str, Any]
) -> None:
    """Mirror the gateway's tutor locally so conversations can reference it.

    The gateway is the source of truth for who the tutor is; this row exists only
    to satisfy referential integrity and to version the persona.
    """
    await session.execute(
        pg_insert(Tutor)
        .values(id=tutor_id, display_name=display_name)
        .on_conflict_do_update(index_elements=[Tutor.id], set_={"display_name": display_name})
    )
    await session.execute(
        pg_insert(TutorPersonaVersion)
        .values(
            tutor_id=tutor_id,
            version=int(persona.get("version", 1)),
            is_active=True,
            persona_json=persona,
        )
        .on_conflict_do_nothing(constraint="uq_persona_tutor_version")
    )


STALE_CLAIM_AFTER = timedelta(minutes=5)
"""A claim older than this is assumed abandoned by a dead container.

Comfortably longer than any legitimate request (provider timeouts cap well below
this), so a live request is never stolen, but short enough that a student whose
container crashed gets an answer on the next redelivery instead of never.
"""


async def claim_idempotency_key(session: AsyncSession, key: str, *, now: datetime) -> bool:
    """Try to claim `key`. Returns True if this caller owns the work.

    The unique index is the arbiter: a concurrent duplicate loses the insert and
    gets False, so work executes exactly once even under a race.

    A claim that is still incomplete and older than STALE_CLAIM_AFTER is taken
    over, because the container that made it is gone and nobody else will finish
    it. A *completed* row is never re-claimed - that is the replay path.
    """
    stale_before = now - STALE_CLAIM_AFTER
    stmt = (
        pg_insert(IdempotencyKey)
        .values(key=key, response_json={}, claimed_at=now)
        .on_conflict_do_update(
            constraint="uq_idempotency_key",
            set_={"claimed_at": now},
            where=(
                IdempotencyKey.completed_at.is_(None) & (IdempotencyKey.claimed_at < stale_before)
            ),
        )
        .returning(IdempotencyKey.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def load_idempotent_response(session: AsyncSession, key: str) -> dict[str, Any] | None:
    """Return a *completed* response, or None.

    Filtering on `completed_at` is what keeps an in-flight claim (whose
    `response_json` is still `{}`) from being replayed as an empty success.
    """
    stmt = select(IdempotencyKey.response_json).where(
        IdempotencyKey.key == key, IdempotencyKey.completed_at.is_not(None)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def store_idempotent_response(
    session: AsyncSession,
    key: str,
    *,
    conversation_id: UUID | None,
    response: dict[str, Any],
    now: datetime,
) -> None:
    """Settle the claim. Setting `completed_at` is what makes it a replayable
    result rather than an in-flight claim."""
    stmt = (
        pg_insert(IdempotencyKey)
        .values(
            key=key,
            conversation_id=conversation_id,
            response_json=response,
            completed_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_idempotency_key",
            set_={
                "response_json": response,
                "conversation_id": conversation_id,
                "completed_at": now,
            },
        )
    )
    await session.execute(stmt)


async def get_or_create_open_conversation(
    session: AsyncSession, *, subject_id: UUID, tutor_id: UUID | None, source: str, now: datetime
) -> Conversation:
    stmt = (
        select(Conversation)
        .where(Conversation.subject_id == subject_id, Conversation.status == "OPEN")
        .order_by(Conversation.last_activity_at.desc())
        .limit(1)
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        existing.last_activity_at = now
        return existing

    conversation = Conversation(
        subject_id=subject_id, tutor_id=tutor_id, source=source, status="OPEN"
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def load_conversation(
    session: AsyncSession, *, conversation_id: UUID, subject_id: UUID
) -> Conversation:
    """Ownership-checked read. Wrong owner raises OwnershipError (404-shaped)."""
    stmt = select(Conversation).where(Conversation.id == conversation_id)
    conversation = (await session.execute(stmt)).scalar_one_or_none()
    if conversation is None or conversation.subject_id != subject_id:
        raise OwnershipError()
    return conversation


async def add_message(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    role: str,
    input_type: str,
    text: str | None,
    media: MediaRef | None = None,
    capability: str | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation_id,
        role=role,
        input_type=input_type,
        text=text,
        media_ref_json=json.loads(media.model_dump_json()) if media else None,
        capability=capability,
    )
    session.add(message)
    await session.flush()
    return message


async def list_messages(
    session: AsyncSession, *, conversation_id: UUID, limit: int = 50
) -> list[Message]:
    """The most recent `limit` messages, oldest-first.

    **Newest selected, oldest-first returned.** Ordering ascending and then
    applying LIMIT takes the OLDEST rows, which is the opposite of what a
    conversation needs: past turn 50 the tutor would be handed the same opening
    exchange forever, never see what the student just said, and pay for those
    stale tokens on every single turn.

    So the selection is descending and the result is reversed in Python. The
    reversal matters as much as the selection - a model given the turns in the
    wrong order reads the student's answers as the questions.
    """
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    newest_first = list((await session.execute(stmt)).scalars())
    newest_first.reverse()
    return newest_first


async def record_request_event(
    session: AsyncSession,
    *,
    event: NormalizedEvent,
    subject_id: UUID | None,
    conversation_id: UUID | None,
) -> RequestEvent:
    row = RequestEvent(
        event_id=event.event_id,
        request_id=event.request_id,
        correlation_id=event.correlation_id,
        source=event.source,
        subject_id=subject_id,
        conversation_id=conversation_id,
        message_type=str(event.message.type),
        contract_version=event.contract_version,
        occurred_at=event.occurred_at,
    )
    session.add(row)
    await session.flush()
    return row


async def record_request_state(
    session: AsyncSession, *, request_event_id: UUID, status: str, error_code: str | None = None
) -> None:
    session.add(
        RequestState(request_event_id=request_event_id, status=status, error_code=error_code)
    )


async def record_outbound_actions(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    request_event_id: UUID | None,
    actions: tuple[OutboundAction, ...],
) -> None:
    for action in actions:
        session.add(
            OutboundActionRow(
                conversation_id=conversation_id,
                request_event_id=request_event_id,
                action_type=str(action.type),
                payload_json=json.loads(action.model_dump_json()),
                delivery_status="PENDING",
            )
        )
