"""Public, unauthenticated endpoints: signing up and paying.

Everything here is reachable by anyone on the internet, so each endpoint states
what stops it being abused:

- `/public/signup` writes one row and calls Cashfree. Rate-limited by the body
  size cap and by the fact that an unpaid signup grants nothing at all.
- `/webhooks/cashfree` is signature-verified. It is the **only** path that
  grants a subscription from a callback.
- `/public/orders/{id}` is a status read the student's browser polls after
  paying. It reveals nothing but whether that order is paid, and it confirms
  against Cashfree rather than trusting the caller.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from tutortwin.api.dependencies import Container, get_container
from tutortwin.db.engine import session_scope
from tutortwin.db.subscription_models import Payment, Signup
from tutortwin.integrations import cashfree
from tutortwin.observability.logging import get_logger
from tutortwin.services import subscriptions

router = APIRouter(tags=["public"])
logger = get_logger(__name__)


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_name: str = Field(min_length=1, max_length=200)
    whatsapp_number: str = Field(min_length=6, max_length=32)
    """Where the agent will run. Normalized to Meta's wa_id form before it is
    stored, so one person cannot arrive as three students."""

    tutor_name: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=120)
    contact_phone: str | None = Field(default=None, max_length=32)
    location: str | None = Field(default=None, max_length=200)


class SignupResponse(BaseModel):
    order_id: str
    payment_session_id: str
    amount_paise: int
    plan_code: str


class OrderStatusResponse(BaseModel):
    order_id: str
    status: str
    activated: bool


@router.post("/public/signup", response_model=SignupResponse)
async def signup(
    payload: SignupRequest,
    container: Container = Depends(get_container),
) -> SignupResponse:
    """Record the form, create a payment order, hand back a checkout session."""
    settings = container.settings
    client = container.cashfree
    if client is None:
        from tutortwin.domain.errors import ErrorCode, TutorTwinError

        raise TutorTwinError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Payments are not configured.")

    wa = subscriptions.normalize_wa_number(payload.whatsapp_number)
    if len(wa) < 10:
        from tutortwin.domain.errors import ErrorCode, TutorTwinError

        raise TutorTwinError(
            ErrorCode.VALIDATION_FAILED, "That WhatsApp number does not look complete."
        )

    order_id = subscriptions.new_order_id()

    async with session_scope() as session:
        record = Signup(
            student_name=payload.student_name.strip(),
            whatsapp_number=wa,
            contact_phone=(payload.contact_phone or "").strip() or None,
            tutor_name=payload.tutor_name.strip(),
            subject=payload.subject.strip(),
            location=(payload.location or "").strip() or None,
            status="PENDING",
        )
        session.add(record)
        await session.flush()

        payment = Payment(
            signup_id=record.id,
            order_id=order_id,
            gateway="cashfree",
            amount_paise=settings.subscription_price_paise,
            plan_code=settings.subscription_plan_code,
            plan_days=settings.subscription_days,
            status="CREATED",
        )
        session.add(payment)
        # Committed before Cashfree is called. If the gateway call fails we
        # still hold the order id and the student's details, which is the
        # difference between "they can retry" and "their form is gone".
        await session.commit()
        signup_id = record.id

    base = settings.public_site_url.rstrip("/")
    order = await client.create_order(
        order_id=order_id,
        amount_paise=settings.subscription_price_paise,
        customer_id=str(signup_id),
        customer_phone=payload.contact_phone or wa,
        customer_name=payload.student_name.strip(),
        return_url=f"{base}/payment/success?order_id={order_id}",
        notify_url=f"{settings.public_api_url.rstrip('/')}/webhooks/cashfree",
    )

    async with session_scope() as session:
        row = (
            await session.execute(select(Payment).where(Payment.order_id == order_id))
        ).scalar_one()
        row.payment_session_id = order.payment_session_id
        await session.commit()

    logger.info("signup_created", order_id=order_id, subject=payload.subject)
    return SignupResponse(
        order_id=order_id,
        payment_session_id=order.payment_session_id,
        amount_paise=settings.subscription_price_paise,
        plan_code=settings.subscription_plan_code,
    )


@router.post("/webhooks/cashfree")
async def cashfree_webhook(
    request: Request,
    container: Container = Depends(get_container),
    signature: str | None = Header(default=None, alias=cashfree.SIGNATURE_HEADER),
    timestamp: str | None = Header(default=None, alias=cashfree.TIMESTAMP_HEADER),
) -> dict[str, Any]:
    """The authority on whether money arrived.

    Returns 200 even when rejecting. Cashfree retries non-2xx, so a 403 here
    invites a forged payload to be redelivered on a schedule; "seen, and not
    acted on" is the correct reply to something that is not from Cashfree.
    """
    settings = container.settings
    secret = settings.cashfree_secret_key
    if secret is None:
        logger.error("cashfree_secret_missing")
        return {"status": "not_configured"}

    body = await request.body()
    try:
        cashfree.verify_webhook(
            body=body,
            signature=signature,
            timestamp=timestamp,
            secret=secret.get_secret_value(),
        )
    except cashfree.SignatureError as exc:
        logger.warning("cashfree_signature_rejected", reason=str(exc))
        return {"status": "rejected"}

    try:
        payload = json.loads(body)
    except ValueError:
        return {"status": "ignored"}
    if not isinstance(payload, dict):
        return {"status": "ignored"}

    order_id = cashfree.order_id_of(payload)
    status = cashfree.order_status_of(payload)
    if not order_id:
        return {"status": "ignored"}

    if status not in cashfree.PAID_STATUSES:
        logger.info("cashfree_webhook_not_paid", order_id=order_id, order_status=status)
        async with session_scope() as session:
            row = (
                await session.execute(select(Payment).where(Payment.order_id == order_id))
            ).scalar_one_or_none()
            if row is not None and row.activated_at is None:
                row.status = "FAILED" if status in {"FAILED", "USER_DROPPED"} else row.status
                row.gateway_payload = payload
                await session.commit()
        return {"status": "ok", "activated": False}

    result = await _activate(container, order_id, payload)
    return {"status": "ok", "activated": result}


@router.get("/public/orders/{order_id}", response_model=OrderStatusResponse)
async def order_status(
    order_id: str,
    container: Container = Depends(get_container),
) -> OrderStatusResponse:
    """Polled by the success page after the student returns from checkout.

    This exists so a subscription still activates promptly when the webhook is
    delayed or the callback URL is briefly unreachable - it asks Cashfree
    directly rather than believing the browser, which knows nothing.
    """
    async with session_scope() as session:
        payment = (
            await session.execute(select(Payment).where(Payment.order_id == order_id))
        ).scalar_one_or_none()
        if payment is None:
            return OrderStatusResponse(order_id=order_id, status="UNKNOWN", activated=False)
        if payment.activated_at is not None:
            return OrderStatusResponse(order_id=order_id, status="PAID", activated=True)

    client = container.cashfree
    if client is None:
        return OrderStatusResponse(order_id=order_id, status="PENDING", activated=False)

    try:
        remote = await client.fetch_order(order_id)
    except cashfree.CashfreeError:
        return OrderStatusResponse(order_id=order_id, status="PENDING", activated=False)

    status = cashfree.order_status_of(remote)
    if status not in cashfree.PAID_STATUSES:
        return OrderStatusResponse(order_id=order_id, status=status, activated=False)

    activated = await _activate(container, order_id, remote)
    return OrderStatusResponse(order_id=order_id, status="PAID", activated=activated)


async def _activate(container: Container, order_id: str, payload: dict[str, Any]) -> bool:
    """Grant the subscription and tell the student, exactly once.

    The notification is sent **after** the transaction commits, deliberately. A
    WhatsApp send is a network call to somebody else's service; holding a
    database transaction open across it would pin a connection for its full
    latency, and a send that fails must not roll back a subscription the
    student has already paid for.
    """
    async with session_scope() as session:
        payment = (
            await session.execute(select(Payment).where(Payment.order_id == order_id))
        ).scalar_one_or_none()
        if payment is None:
            logger.warning("cashfree_unknown_order", order_id=order_id)
            return False

        result = await subscriptions.activate_payment(
            session,
            payment,
            gateway_payload=payload,
            payment_reference=cashfree.payment_reference_of(payload),
        )
        await session.commit()

    if not result.activated:
        return False

    await notify_activation(container, result)
    return True


async def notify_activation(
    container: Container, result: subscriptions.ActivationResult
) -> None:
    """Tell the student on WhatsApp that they are live.

    Sent as a **template**, because a confirmation almost always falls outside
    Meta's 24-hour customer service window - the student paid on a website, they
    were not mid-conversation - and a plain text message there is rejected with
    a 400 that nobody sees.
    """
    client = container.whatsapp
    if client is None or not result.whatsapp_number:
        logger.info("activation_notification_skipped", reason="whatsapp not configured")
        return

    await client.send_template(
        result.whatsapp_number,
        name=subscriptions.ACTIVATION_TEMPLATE,
        body_params=(
            result.student_name or "there",
            result.tutor_name or "TutorTwin",
        ),
    )
    logger.info("activation_notified", subject_id=str(result.subject_id))
