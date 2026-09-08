"""Deterministic placeholder capability.

Phase 02 replaces this with the real capability router. What must survive that
replacement is the shape: a pure function from event to outbound actions, making
zero provider calls.

It already honours the media brief gate, because that rule is cost policy rather
than AI behaviour: media with no accompanying instruction is answered with a
request for a brief, not by processing the file.
"""

from __future__ import annotations

from tutortwin.domain.events import (
    MessageType,
    NormalizedEvent,
    OutboundAction,
    OutboundActionType,
)
from tutortwin.domain.models import TutorProfile

MEDIA_TYPES = frozenset(
    {MessageType.IMAGE, MessageType.PDF, MessageType.DOCUMENT, MessageType.AUDIO}
)

BRIEF_PROMPT = (
    "I can see your attachment. Tell me what you would like me to do with it - "
    'for example "solve question 4" or "summarize pages 2-3".'
)


def has_brief(event: NormalizedEvent) -> bool:
    """A brief is any non-trivial caption sent with the media."""
    text = (event.message.text or "").strip()
    return len(text) >= 3


def build_placeholder_reply(
    event: NormalizedEvent, tutor: TutorProfile | None
) -> tuple[OutboundAction, ...]:
    if event.message.type in MEDIA_TYPES and not has_brief(event):
        # Cost gate: no OCR, no vision, no embedding, no download.
        return (OutboundAction(type=OutboundActionType.ASK_FILE_BRIEF, text=BRIEF_PROMPT),)

    identity = tutor.assistant_identity if tutor else "TutorTwin - AI Assistant"
    received = (event.message.text or "").strip()
    body = (
        f"{identity} received your message."
        if not received
        else f"{identity} received your message ({len(received)} characters)."
    )
    return (OutboundAction(type=OutboundActionType.SEND_TEXT, text=body),)
