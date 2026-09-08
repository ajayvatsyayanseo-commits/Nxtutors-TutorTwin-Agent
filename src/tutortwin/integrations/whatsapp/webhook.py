"""Inbound side of the Meta webhook: prove it is Meta, then normalize it.

Two jobs, in that order. Nothing here trusts the payload before
`verify_signature` has run, because until then the payload is simply whatever
arrived at a public URL.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from tutortwin.domain.events import (
    EventContext,
    InboundMessage,
    MediaRef,
    MessageType,
    NormalizedEvent,
    SubjectRef,
)
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

SOURCE = "whatsapp"
PROVIDER = "whatsapp"

SIGNATURE_HEADER = "x-hub-signature-256"
_PREFIX = "sha256="


class SignatureError(Exception):
    """The payload did not come from Meta, or was altered on the way."""


def verify_signature(*, body: bytes, header: str | None, app_secret: str) -> None:
    """HMAC-SHA256 of the **raw** body, keyed by the app secret.

    Raw matters: re-serializing the parsed JSON changes key order and spacing,
    and the signature covers bytes, so a re-serialized body never validates.

    This raises rather than returning a bool. A caller that forgets to check a
    returned bool has an open endpoint; a caller that forgets to catch an
    exception has a 500. Only one of those leaks student data.
    """
    if not app_secret:
        raise SignatureError("no app secret configured")
    if not header or not header.startswith(_PREFIX):
        raise SignatureError("missing or malformed signature header")

    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header[len(_PREFIX) :].strip()):
        raise SignatureError("signature mismatch")


def verify_challenge(
    *, mode: str | None, token: str | None, challenge: str | None, verify_token: str
) -> str:
    """Meta's GET handshake during webhook registration.

    Returns the challenge to echo back. `compare_digest` here too: the token is
    a shared secret, and a plain `==` leaks its length and prefix through timing.
    """
    if mode != "subscribe" or not challenge:
        raise SignatureError("not a subscribe challenge")
    if not token or not verify_token or not hmac.compare_digest(token, verify_token):
        raise SignatureError("verify token mismatch")
    return challenge


# Meta's message `type` to TutorTwin's. `video` has no dedicated member and
# travels as DOCUMENT: the media pipeline rejects it on MIME anyway, and adding
# a member the rest of the product does not handle would only move the failure.
_TYPES: dict[str, MessageType] = {
    "text": MessageType.TEXT,
    "image": MessageType.IMAGE,
    "audio": MessageType.AUDIO,
    "voice": MessageType.AUDIO,
    "document": MessageType.DOCUMENT,
    "video": MessageType.DOCUMENT,
}


def _text_of(message: dict[str, Any], kind: str) -> str | None:
    """The words the student actually typed, wherever Meta put them."""
    if kind == "text":
        body = message.get("text", {}).get("body")
        return str(body) if body else None
    if kind == "interactive":
        block = message.get("interactive", {})
        reply = block.get("button_reply") or block.get("list_reply") or {}
        title = reply.get("title") or reply.get("id")
        return str(title) if title else None
    if kind == "button":
        payload = message.get("button", {})
        title = payload.get("text") or payload.get("payload")
        return str(title) if title else None
    # A caption on a photo *is* the question. Dropping it turns "solve Q3 only"
    # into a bare image and makes the brief gate ask for what was already sent.
    block = message.get(kind)
    caption = block.get("caption") if isinstance(block, dict) else None
    return str(caption) if caption else None


def _media_of(message: dict[str, Any], kind: str) -> MediaRef | None:
    block = message.get(kind)
    if not isinstance(block, dict) or not block.get("id"):
        return None
    size = block.get("file_size")
    return MediaRef(
        provider=PROVIDER,
        media_id=str(block["id"])[:256],
        mime_type_hint=(str(block["mime_type"])[:255] if block.get("mime_type") else None),
        # Meta sends this as an int, older payloads as a string. A hint that
        # cannot be trusted is left unset rather than guessed at; the pipeline
        # measures the real bytes regardless.
        size_hint=int(size) if isinstance(size, int | str) and str(size).isdigit() else None,
        filename=(str(block["filename"])[:512] if block.get("filename") else None),
    )


def normalize(payload: dict[str, Any]) -> list[NormalizedEvent]:
    """Meta's batched envelope into zero or more TutorTwin events.

    Zero is the common case, not an error: most callbacks are delivery and read
    receipts (`statuses`), which carry no student message. Turning those into
    events would open a conversation turn every time a phone ticks.

    Processed per message rather than per batch, because one unparseable entry
    among five must not discard the other four.
    """
    events: list[NormalizedEvent] = []

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                event = _normalize_one(message)
                if event is not None:
                    events.append(event)

    return events


def _normalize_one(message: dict[str, Any]) -> NormalizedEvent | None:
    kind = str(message.get("type") or "")
    wa_id = message.get("from")
    wamid = message.get("id")
    if not wa_id or not wamid:
        return None

    text = _text_of(message, kind)
    media = _media_of(message, kind) if kind in _TYPES and kind != "text" else None

    if kind in ("interactive", "button"):
        message_type = MessageType.ACTION
    else:
        message_type = _TYPES.get(kind, MessageType.SYSTEM)

    # A sticker, a location pin, a shared contact: nothing to tutor from, and no
    # media the pipeline would accept. Silence beats an error message here.
    if message_type is MessageType.SYSTEM:
        logger.info("whatsapp_unsupported_message", kind=kind)
        return None
    if message_type is not MessageType.TEXT and media is None and not text:
        return None

    # PDFs get their own type so the extractor routes them without sniffing.
    if message_type is MessageType.DOCUMENT and media and media.mime_type_hint == "application/pdf":
        message_type = MessageType.PDF

    raw_ts = str(message.get("timestamp") or "")
    occurred_at = (
        datetime.fromtimestamp(int(raw_ts), tz=UTC) if raw_ts.isdigit() else datetime.now(UTC)
    )

    wamid = str(wamid)[:128]
    return NormalizedEvent(
        event_id=wamid,
        # All three are the message id deliberately. It is Meta's unique id for
        # this message, so a redelivered callback lands on the same idempotency
        # key and replays the stored answer instead of paying for a second one.
        request_id=wamid,
        correlation_id=wamid,
        source=SOURCE,
        source_agent="whatsapp_cloud_api",
        subject=SubjectRef(external_type=SOURCE, external_id=str(wa_id)[:256]),
        message=InboundMessage(
            message_id=wamid,
            type=message_type,
            text=text[:16_000] if text else None,
            media=media,
        ),
        context=EventContext(locale="en-IN"),
        occurred_at=occurred_at,
    )


__all__ = [
    "PROVIDER",
    "SIGNATURE_HEADER",
    "SOURCE",
    "SignatureError",
    "normalize",
    "verify_challenge",
    "verify_signature",
]
