"""The Meta WhatsApp webhook.

This is the one route on the service that is authenticated by signature rather
than by a shared header, because Meta decides what it sends and cannot be asked
to carry ours.

Two endpoints, both required by Meta:

`GET`  - the subscribe handshake, run once when the webhook is registered and
         again whenever the callback URL changes.
`POST` - every inbound message, delivery receipt and read receipt, batched.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import PlainTextResponse

from tutortwin.api.dependencies import Container, get_container
from tutortwin.domain.events import NormalizedEvent
from tutortwin.integrations.whatsapp import webhook as wa
from tutortwin.observability.logging import event_id_var, get_logger

router = APIRouter(tags=["whatsapp"])
logger = get_logger(__name__)


@router.get("/webhooks/whatsapp")
async def verify_webhook(
    request: Request,
    container: Container = Depends(get_container),
) -> Response:
    """Meta's subscribe handshake. Echo the challenge, as plain text.

    The parameter names carry dots (`hub.mode`), which is not a legal Python
    identifier, so they are read from the query mapping rather than declared as
    function arguments.
    """
    token = container.settings.whatsapp_verify_token
    if token is None:
        logger.error("whatsapp_verify_token_missing")
        return PlainTextResponse("not configured", status_code=503)

    params = request.query_params
    try:
        challenge = wa.verify_challenge(
            mode=params.get("hub.mode"),
            token=params.get("hub.verify_token"),
            challenge=params.get("hub.challenge"),
            verify_token=token.get_secret_value(),
        )
    except wa.SignatureError as exc:
        logger.warning("whatsapp_verify_rejected", reason=str(exc))
        return PlainTextResponse("forbidden", status_code=403)

    logger.info("whatsapp_verify_ok")
    return PlainTextResponse(challenge)


@router.post("/webhooks/whatsapp")
async def receive_webhook(
    request: Request,
    container: Container = Depends(get_container),
    signature: str | None = Header(default=None, alias=wa.SIGNATURE_HEADER),
) -> dict[str, Any]:
    """Verify, normalize, answer.

    Answered inline rather than handed to a background task. Cloud Run bills by
    request and throttles CPU between them, so work started after the response
    is written may simply not run - and a webhook that acknowledges a message it
    then drops is worse than a slow one.

    ponytail: inline turn processing. If Meta starts redelivering because a
    model call ran long, move the work onto the existing Cloud Tasks queue; the
    idempotency key below already makes a redelivery harmless in the meantime.
    """
    settings = container.settings
    secret = settings.whatsapp_app_secret
    if secret is None:
        # Refuse rather than accept unverified messages. An unauthenticated
        # webhook lets anyone spend the model budget and write into a student's
        # conversation history.
        logger.error("whatsapp_app_secret_missing")
        return {"status": "not_configured"}

    body = await request.body()
    try:
        wa.verify_signature(body=body, header=signature, app_secret=secret.get_secret_value())
    except wa.SignatureError as exc:
        logger.warning("whatsapp_signature_rejected", reason=str(exc))
        # 200 on purpose. Meta retries non-2xx, so a 403 here invites a forged
        # payload to be redelivered on a backoff schedule. Rejected and
        # acknowledged is the right answer to a request that is not from Meta.
        return {"status": "rejected"}

    import json

    try:
        payload = json.loads(body)
    except ValueError:
        logger.warning("whatsapp_payload_not_json")
        return {"status": "ignored"}

    events = wa.normalize(payload if isinstance(payload, dict) else {})
    if not events:
        # Delivery and read receipts land here. Extremely common, not a problem.
        return {"status": "ignored", "handled": 0}

    handled = 0
    for event in events:
        if await _handle_one(container, event):
            handled += 1

    return {"status": "ok", "handled": handled}


async def _handle_one(container: Container, event: NormalizedEvent) -> bool:
    """One message, fully processed. Never raises.

    A batch can carry several messages from several students. One that fails
    must not take the rest of the batch with it, and must not fail the response
    to Meta - which would redeliver every message in the batch, including the
    ones already answered.
    """
    token = event_id_var.set(event.event_id)
    try:
        response = await container.entry_service.handle_event(event)
        logger.info(
            "whatsapp_turn_handled",
            status=response.status,
            replay=response.idempotent_replay,
            paid_calls=response.usage.paid_model_calls,
        )
        return True
    except Exception as exc:
        logger.error(
            "whatsapp_turn_failed",
            error_type=type(exc).__name__,
            message_id=event.message.message_id,
        )
        return False
    finally:
        event_id_var.reset(token)
