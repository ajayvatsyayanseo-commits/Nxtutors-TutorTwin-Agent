"""Turning a payment into a working tutor.

Activation is the one operation in this product that must be exactly-once. It
grants access, it creates the persona, and it sends a message the student reads
as a receipt. Doing it twice gives away a second month and messages the student
twice; doing it zero times takes their money and leaves them with nothing.

So the whole thing hangs off one column: `payments.activated_at`. It is set
inside the same transaction that writes the entitlement, and every entry point
checks it first. Cashfree delivers webhooks at least once and the student's
browser also triggers a status check on return, so **three concurrent
activations of the same order is the normal case, not the edge case.**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.models import Entitlement, Subject, Tutor, TutorAssignment, TutorPersonaVersion
from tutortwin.db.subscription_models import Payment, Signup
from tutortwin.observability.logging import get_logger
from tutortwin.providers.fakes import deterministic_uuid
from tutortwin.services.entitlements import ACTIVE

logger = get_logger(__name__)

WHATSAPP_IDENTITY = "whatsapp"

ACTIVATION_TEMPLATE = "subscription_activated"
"""Must match the template name approved in the Meta console. A confirmation
almost always falls outside the 24-hour window, so a plain text message here
would be rejected and the student would never learn their subscription is live."""


def normalize_wa_number(raw: str) -> str:
    """To the digits-only form Meta uses as a `wa_id`.

    `+91 99990 00001`, `919999000001` and `09999000001` are one person. Storing
    them as typed makes three students, of whom two have paid for a
    subscription that is attached to a number the agent will never see a
    message from.
    """
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    # A bare Indian mobile, with or without the trunk 0. Meta always wants the
    # country code, and a student typing their own number rarely includes it.
    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    return digits


@dataclass(slots=True)
class ActivationResult:
    activated: bool
    """False means already activated. Not an error - it is the idempotency
    guard doing its job, and the caller should still answer 200."""

    subject_id: UUID | None = None
    plan_code: str | None = None
    ends_at: datetime | None = None
    student_name: str | None = None
    whatsapp_number: str | None = None
    tutor_name: str | None = None


def canonical_subject_id(wa_number: str) -> UUID:
    """The one id a WhatsApp number maps to, everywhere.

    **This must equal what `IdentityGateway.resolve()` returns for the same
    number, and a test asserts that it does.** The two derivations drifting
    apart is not a theoretical risk - it is a bug this code already had, and it
    was the most expensive kind:

    activation created the `tutortwin_subjects` row with a random `uuid4()` and
    wrote the entitlement against that id, while an inbound message resolved the
    same student to a deterministic uuid5. The entitlement lookup used the
    resolver's id, found nothing, and every paying student was treated as
    unsubscribed. The money moved and the access did not.
    """
    return deterministic_uuid("subject", WHATSAPP_IDENTITY, wa_number)


async def get_or_create_subject(session: AsyncSession, wa_number: str, name: str | None) -> Subject:
    """The student's tutoring identity, keyed on their WhatsApp number.

    Created here rather than at signup on purpose: an unpaid signup should not
    mint an identity that later looks like a real student in the admin console.
    """
    existing = (
        await session.execute(
            select(Subject).where(
                Subject.external_identity_type == WHATSAPP_IDENTITY,
                Subject.external_identity_value == wa_number,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if name and not existing.display_name:
            existing.display_name = name
        return existing

    subject = Subject(
        # NOT the model's `default=uuid4`. The id has to be the one the identity
        # gateway will resolve this number to, or the entitlement written below
        # is attached to a student the runtime never asks about.
        id=canonical_subject_id(wa_number),
        external_identity_type=WHATSAPP_IDENTITY,
        external_identity_value=wa_number,
        display_name=name,
        status="ACTIVE",
    )
    session.add(subject)
    await session.flush()
    return subject


async def assign_named_tutor(session: AsyncSession, subject: Subject, tutor_name: str) -> Tutor:
    """Give the agent the name the student asked for.

    This is a persona, never an impersonation. The agent answers to the name and
    adopts a teaching style; it does not claim to be that person, and the
    persona's `forbidden_behaviors` says so explicitly so the instruction
    survives into the system prompt rather than living only in this comment.
    """
    tutor = (
        await session.execute(select(Tutor).where(Tutor.display_name == tutor_name).limit(1))
    ).scalar_one_or_none()

    if tutor is None:
        tutor = Tutor(display_name=tutor_name, status="ACTIVE")
        session.add(tutor)
        await session.flush()
        session.add(
            TutorPersonaVersion(
                tutor_id=tutor.id,
                version=1,
                is_active=True,
                persona_json={
                    "version": 1,
                    "tone": "supportive",
                    "hint_first": True,
                    "step_by_step": True,
                    "forbidden_behaviors": [
                        "Never claim to be the human teacher this persona is named after.",
                        "Never state or imply that a human is reading the conversation.",
                    ],
                },
            )
        )

    already = (
        await session.execute(
            select(TutorAssignment).where(
                TutorAssignment.subject_id == subject.id,
                TutorAssignment.tutor_id == tutor.id,
                TutorAssignment.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if already is None:
        session.add(TutorAssignment(subject_id=subject.id, tutor_id=tutor.id, is_active=True))

    return tutor


async def activate_payment(
    session: AsyncSession,
    payment: Payment,
    *,
    now: datetime | None = None,
    gateway_payload: dict[str, object] | None = None,
    payment_reference: str | None = None,
) -> ActivationResult:
    """Grant the subscription. Safe to call repeatedly for the same payment.

    Everything below happens in the caller's transaction, so the entitlement
    and the `activated_at` stamp commit together or not at all. A crash halfway
    cannot leave a student entitled with no record of why, or a payment marked
    activated with no entitlement behind it.
    """
    now = now or datetime.now(UTC)

    if payment.activated_at is not None:
        logger.info("payment_already_activated", order_id=payment.order_id)
        return ActivationResult(activated=False)

    signup = (
        await session.execute(select(Signup).where(Signup.id == payment.signup_id))
    ).scalar_one_or_none()
    if signup is None:
        # Referential integrity says this cannot happen; if it ever does, an
        # entitlement with nobody to attach it to is not something to guess at.
        logger.error("payment_without_signup", order_id=payment.order_id)
        return ActivationResult(activated=False)

    subject = await get_or_create_subject(session, signup.whatsapp_number, signup.student_name)
    await assign_named_tutor(session, subject, signup.tutor_name)

    # Supersede rather than delete. Who had what, when, is what a billing
    # dispute is settled with, and a renewal must not erase the previous term.
    current = (
        await session.execute(
            select(Entitlement).where(
                Entitlement.subject_id == subject.id, Entitlement.status == ACTIVE
            )
        )
    ).scalars().all()
    for row in current:
        row.status = "SUPERSEDED"

    ends_at = now + timedelta(days=payment.plan_days)
    session.add(
        Entitlement(
            subject_id=subject.id,
            plan_code=payment.plan_code,
            status=ACTIVE,
            starts_at=now,
            ends_at=ends_at,
            source="payment",
            source_version=payment.order_id,
            metadata_json={
                "order_id": payment.order_id,
                "amount_paise": payment.amount_paise,
                "gateway": payment.gateway,
                "payment_reference": payment_reference,
            },
        )
    )

    payment.status = "PAID"
    payment.activated_at = now
    if payment_reference:
        payment.gateway_payment_id = payment_reference
    if gateway_payload:
        payment.gateway_payload = gateway_payload

    signup.status = "PAID"
    signup.subject_id = subject.id

    logger.info(
        "subscription_activated",
        order_id=payment.order_id,
        subject_id=str(subject.id),
        plan_code=payment.plan_code,
        ends_at=ends_at.isoformat(),
    )
    return ActivationResult(
        activated=True,
        subject_id=subject.id,
        plan_code=payment.plan_code,
        ends_at=ends_at,
        student_name=signup.student_name,
        whatsapp_number=signup.whatsapp_number,
        tutor_name=signup.tutor_name,
    )


async def grant_manual_subscription(
    session: AsyncSession,
    *,
    whatsapp_number: str,
    student_name: str,
    tutor_name: str,
    subject_name: str,
    plan_code: str,
    days: int,
    granted_by: str,
    now: datetime | None = None,
) -> ActivationResult:
    """An operator gives somebody a subscription without a payment.

    Written through the same path as a paid activation - same tables, same
    supersede rule, same notification - so a comped subscription behaves
    identically to a bought one everywhere downstream. The only difference is
    `source`, which is what tells an operator later that no money was involved.
    """
    now = now or datetime.now(UTC)
    wa = normalize_wa_number(whatsapp_number)

    signup = Signup(
        student_name=student_name,
        whatsapp_number=wa,
        tutor_name=tutor_name,
        subject=subject_name,
        status="PAID",
    )
    session.add(signup)
    await session.flush()

    subject = await get_or_create_subject(session, wa, student_name)
    await assign_named_tutor(session, subject, tutor_name)
    signup.subject_id = subject.id

    current = (
        await session.execute(
            select(Entitlement).where(
                Entitlement.subject_id == subject.id, Entitlement.status == ACTIVE
            )
        )
    ).scalars().all()
    for row in current:
        row.status = "SUPERSEDED"

    ends_at = now + timedelta(days=days)
    session.add(
        Entitlement(
            subject_id=subject.id,
            plan_code=plan_code,
            status=ACTIVE,
            starts_at=now,
            ends_at=ends_at,
            source="admin_grant",
            source_version=granted_by,
            metadata_json={"granted_by": granted_by, "days": days},
        )
    )

    logger.info(
        "subscription_granted_manually",
        subject_id=str(subject.id),
        plan_code=plan_code,
        granted_by=granted_by,
    )
    return ActivationResult(
        activated=True,
        subject_id=subject.id,
        plan_code=plan_code,
        ends_at=ends_at,
        student_name=student_name,
        whatsapp_number=wa,
        tutor_name=tutor_name,
    )


def new_order_id() -> str:
    """Ours, not the gateway's, and generated before the gateway is called.

    If the create-order request times out we still hold the id, so the order can
    be looked up rather than silently retried into a double charge.
    """
    return f"tt_{uuid4().hex[:24]}"


__all__ = [
    "ACTIVATION_TEMPLATE",
    "ActivationResult",
    "activate_payment",
    "assign_named_tutor",
    "get_or_create_subject",
    "grant_manual_subscription",
    "new_order_id",
    "normalize_wa_number",
]
