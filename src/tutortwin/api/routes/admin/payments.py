"""Signups and payments: who asked for the product, and who paid for it.

Both tables were written by the public site and read by nobody. An operator
could grant a subscription but could not see a single order, which makes the
ordinary questions unanswerable: did this student's payment arrive, how many
people filled in the form and never paid, what did we take this week.

Read-only, deliberately. Money is corrected at the gateway and re-applied
through the webhook, never by editing a row here - a `payments` row that
disagrees with Cashfree is worse than no row at all. The one write an operator
needs, granting access without a payment, already exists on the students router
and records a high-risk audit entry.

Amounts are integer paise everywhere, converted for display only at the edge.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from tutortwin.api.admin_deps import DbSession, require
from tutortwin.db.subscription_models import Payment, Signup
from tutortwin.domain.admin import AdminActor, Permission
from tutortwin.domain.errors import ErrorCode, TutorTwinError
from tutortwin.observability.logging import get_logger
from tutortwin.repositories import admin as admin_repo

router = APIRouter()
logger = get_logger(__name__)

PAID = "PAID"


class PaymentView(BaseModel):
    id: str
    order_id: str
    gateway: str
    gateway_payment_id: str | None
    amount_paise: int
    currency: str
    plan_code: str
    plan_days: int
    status: str
    activated_at: datetime | None
    """Null on a PAID row means the money arrived and the entitlement did not.

    That combination is the one an operator must be able to find: it is a
    student who has been charged and has no access."""
    created_at: datetime

    student_name: str
    whatsapp_number: str
    contact_phone: str | None
    tutor_name: str
    subject: str
    location: str | None
    signup_id: str
    signup_status: str
    subject_id: str | None


class PaymentPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[PaymentView]


class SignupView(BaseModel):
    id: str
    student_name: str
    whatsapp_number: str
    contact_phone: str | None
    tutor_name: str
    subject: str
    location: str | None
    status: str
    subject_id: str | None
    created_at: datetime
    payment_count: int
    paid: bool


class SignupPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[SignupView]


class RevenueSummary(BaseModel):
    """Counted, not estimated. Every figure is a row count or a SUM."""

    signups: int
    paid_signups: int
    abandoned_signups: int
    """Filled in the form, never paid. A lead, not an error - which is why the
    two tables are separate rows in the first place."""

    orders: int
    paid_orders: int
    gross_paise: int
    """PAID orders only. A CREATED order is an intention, not money."""

    awaiting_activation: int
    """PAID with no `activated_at`. Should always be zero; anything else is a
    student who paid and cannot use the product, and is the single most
    urgent number on this page."""


def _payment_view(payment: Payment, signup: Signup) -> PaymentView:
    return PaymentView(
        id=str(payment.id),
        order_id=payment.order_id,
        gateway=payment.gateway,
        gateway_payment_id=payment.gateway_payment_id,
        amount_paise=payment.amount_paise,
        currency=payment.currency,
        plan_code=payment.plan_code,
        plan_days=payment.plan_days,
        status=payment.status,
        activated_at=payment.activated_at,
        created_at=payment.created_at,
        student_name=signup.student_name,
        whatsapp_number=signup.whatsapp_number,
        contact_phone=signup.contact_phone,
        tutor_name=signup.tutor_name,
        subject=signup.subject,
        location=signup.location,
        signup_id=str(signup.id),
        signup_status=signup.status,
        subject_id=str(signup.subject_id) if signup.subject_id else None,
    )


@router.get("/payments/summary", response_model=RevenueSummary)
async def revenue_summary(
    db: DbSession,
    _: AdminActor = Depends(require(Permission.PAYMENT_READ)),
) -> RevenueSummary:
    signups = int((await db.execute(select(func.count()).select_from(Signup))).scalar_one() or 0)
    paid_signups = int(
        (
            await db.execute(select(func.count()).select_from(Signup).where(Signup.status == PAID))
        ).scalar_one()
        or 0
    )
    orders = int((await db.execute(select(func.count()).select_from(Payment))).scalar_one() or 0)
    paid_orders, gross = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(Payment.amount_paise), 0)).where(
                Payment.status == PAID
            )
        )
    ).one()
    stranded = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Payment)
                .where(Payment.status == PAID, Payment.activated_at.is_(None))
            )
        ).scalar_one()
        or 0
    )

    if stranded:
        # Worth a log line as well as a number on a page: this is money taken
        # for access that was never granted, and nobody may be looking.
        logger.warning("payments_awaiting_activation", count=stranded)

    return RevenueSummary(
        signups=signups,
        paid_signups=paid_signups,
        abandoned_signups=signups - paid_signups,
        orders=orders,
        paid_orders=int(paid_orders or 0),
        gross_paise=int(gross or 0),
        awaiting_activation=stranded,
    )


@router.get("/payments", response_model=PaymentPage)
async def list_payments(
    db: DbSession,
    status: str | None = Query(default=None, max_length=24),
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=admin_repo.DEFAULT_PAGE_SIZE, ge=1, le=admin_repo.MAX_PAGE_SIZE),
    _: AdminActor = Depends(require(Permission.PAYMENT_READ)),
) -> PaymentPage:
    # Inner join: a payment without its signup cannot exist (the foreign key
    # cascades), and showing a half-row would be worse than showing none.
    base = select(Payment, Signup).join(Signup, Signup.id == Payment.signup_id)

    if status:
        base = base.where(Payment.status == status)
    if q:
        # Escaped: a search term containing `%` must match a literal percent
        # sign, not every order in the table.
        pattern = admin_repo.like_pattern(q.strip())
        base = base.where(
            Signup.student_name.ilike(pattern, escape="\\")
            | Signup.whatsapp_number.ilike(pattern, escape="\\")
            | Payment.order_id.ilike(pattern, escape="\\")
        )

    total = await admin_repo.count_rows(db, base)
    rows = (
        await db.execute(
            admin_repo.paginate(
                base.order_by(Payment.created_at.desc()), page=page, page_size=page_size
            )
        )
    ).all()

    return PaymentPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[_payment_view(payment, signup) for payment, signup in rows],
    )


@router.get("/payments/{payment_id}", response_model=PaymentView)
async def payment_detail(
    payment_id: UUID,
    db: DbSession,
    _: AdminActor = Depends(require(Permission.PAYMENT_READ)),
) -> PaymentView:
    row = (
        await db.execute(
            select(Payment, Signup)
            .join(Signup, Signup.id == Payment.signup_id)
            .where(Payment.id == payment_id)
        )
    ).first()
    if row is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Payment not found.")
    # `gateway_payload` is deliberately NOT returned. It is kept verbatim for
    # disputes and can contain contact details the gateway echoed back; an
    # operator settling one reads it from the database with a reason to.
    return _payment_view(*row)


@router.get("/signups", response_model=SignupPage)
async def list_signups(
    db: DbSession,
    status: str | None = Query(default=None, max_length=24),
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=admin_repo.DEFAULT_PAGE_SIZE, ge=1, le=admin_repo.MAX_PAGE_SIZE),
    _: AdminActor = Depends(require(Permission.PAYMENT_READ)),
) -> SignupPage:
    attempts = (
        select(Payment.signup_id, func.count().label("attempts"))
        .group_by(Payment.signup_id)
        .subquery()
    )
    base = select(Signup, func.coalesce(attempts.c.attempts, 0)).outerjoin(
        attempts, attempts.c.signup_id == Signup.id
    )

    if status:
        base = base.where(Signup.status == status)
    if q:
        pattern = admin_repo.like_pattern(q.strip())
        base = base.where(
            Signup.student_name.ilike(pattern, escape="\\")
            | Signup.whatsapp_number.ilike(pattern, escape="\\")
        )

    total = await admin_repo.count_rows(db, base)
    rows = (
        await db.execute(
            admin_repo.paginate(
                base.order_by(Signup.created_at.desc()), page=page, page_size=page_size
            )
        )
    ).all()

    return SignupPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[
            SignupView(
                id=str(signup.id),
                student_name=signup.student_name,
                whatsapp_number=signup.whatsapp_number,
                contact_phone=signup.contact_phone,
                tutor_name=signup.tutor_name,
                subject=signup.subject,
                location=signup.location,
                status=signup.status,
                subject_id=str(signup.subject_id) if signup.subject_id else None,
                created_at=signup.created_at,
                payment_count=int(count or 0),
                paid=signup.status == PAID,
            )
            for signup, count in rows
        ],
    )
