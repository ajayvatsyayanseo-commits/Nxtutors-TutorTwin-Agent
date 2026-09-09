"""Subscription and payment tables.

Kept in their own module rather than added to `models.py` because they are the
only tables that hold **commercial** rather than pedagogical state, and because
a payment row has a different retention story from a conversation: a
conversation can be deleted on request, a payment record generally cannot.

The signup form and the payment are separate rows on purpose. A person who
fills in the form and abandons the payment page is a real, useful thing to know
about - they are a lead, not an error - and folding the two together would lose
that the moment the payment failed.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tutortwin.db.models import Base


class Signup(Base):
    """What the student typed into the form, before any money moved.

    The WhatsApp number is the identity that matters: it is where the agent
    runs, so it is what everything downstream keys on. The contact phone is
    kept separate because they are genuinely often different numbers, and
    messaging the wrong one means the student never hears from us.
    """

    __tablename__ = "signups"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    student_name: Mapped[str] = mapped_column(String(200), nullable=False)
    whatsapp_number: Mapped[str] = mapped_column(String(32), nullable=False)
    """E.164 digits, no plus, no spaces - the wa_id format Meta uses. Normalized
    on the way in so the same person cannot arrive as three different students."""

    contact_phone: Mapped[str | None] = mapped_column(String(32))
    tutor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    """The teacher's name the student wants the agent to answer to. TutorTwin
    never claims to *be* that teacher - the persona is styled, not impersonated."""

    subject: Mapped[str] = mapped_column(String(120), nullable=False)
    location: Mapped[str | None] = mapped_column(String(200))

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    """PENDING -> PAID, or PENDING forever. An abandoned signup is a lead."""

    subject_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tutortwin_subjects.id", ondelete="SET NULL")
    )
    """Filled in at activation. Null before that, because a student who never
    paid should not create a tutoring identity that counts against anything."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_signup_whatsapp", "whatsapp_number"),
        Index("ix_signup_status_created", "status", "created_at"),
    )


class Payment(Base):
    """One attempt to pay. Not one subscription - those are `entitlements`.

    Money is the one place where being wrong twice is much worse than being
    wrong once, so this table is written defensively:

    - `order_id` is unique, so a retried webhook cannot create a second row.
    - `amount_paise` is an integer. Rupees as a float would eventually pay
      someone 99.99999 rupees, and the rounding argument that follows is not
      one anybody wants to have with a customer.
    - the raw gateway payload is kept verbatim in `gateway_payload`, because
      when a payment is disputed the only thing worth having is exactly what
      the gateway actually said, not our summary of it.
    """

    __tablename__ = "payments"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    signup_id: Mapped[UUID] = mapped_column(
        ForeignKey("signups.id", ondelete="CASCADE"), nullable=False
    )

    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    """Ours, not the gateway's. We generate it so the order can be looked up
    even if the gateway call fails before returning anything."""

    gateway: Mapped[str] = mapped_column(String(32), nullable=False, default="cashfree")
    gateway_payment_id: Mapped[str | None] = mapped_column(String(128))

    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    plan_code: Mapped[str] = mapped_column(String(64), nullable=False, default="PRO")
    plan_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="CREATED")
    """CREATED -> PAID | FAILED | EXPIRED. Only PAID grants anything."""

    payment_session_id: Mapped[str | None] = mapped_column(Text)
    gateway_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)

    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Set exactly once, when the entitlement is written. Its presence is what
    makes activation idempotent: a webhook Cashfree delivers three times must
    grant one subscription and send one WhatsApp message, not three."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("order_id", name="uq_payment_order"),
        Index("ix_payment_signup", "signup_id"),
        Index("ix_payment_status", "status", "created_at"),
    )


__all__ = ["Payment", "Signup"]
