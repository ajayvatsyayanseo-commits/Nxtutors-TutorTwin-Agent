"""Outbound side of the Meta integration: the Graph API, and the two ports
TutorTwin drives it through.

`WhatsAppMediaSource` implements `MediaSource` so a photo of a homework page
becomes bytes the existing OCR pipeline can read. `WhatsAppOutboundGateway`
implements `OutboundGateway` so an answer reaches a phone. Neither knows
anything about tutoring, and orchestration knows nothing about Meta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from tutortwin.domain.events import OutboundAction, OutboundActionType
from tutortwin.domain.models import ResolvedSubject
from tutortwin.media.adapters import MediaNotFound, MediaSource
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

GRAPH_HOST = "https://graph.facebook.com"

# Meta rejects a text body over 4096 characters outright. A worked solution to a
# multi-part problem passes that easily, so long answers are split rather than
# truncated - a maths answer cut mid-derivation is worse than two messages.
TEXT_LIMIT = 4096

# Documents and images are sent by link, and Meta fetches the link itself. Only
# these actions carry one; everything else is prose.
_MEDIA_ACTIONS = {
    OutboundActionType.SEND_IMAGE: "image",
    OutboundActionType.SEND_DOCUMENT: "document",
    OutboundActionType.SEND_AUDIO: "audio",
}


def _chunk(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split on a paragraph or line boundary when one is near the limit.

    A hard slice every 4096 bytes would cut mid-word and mid-equation. This
    gives back the same text either way; it only chooses where to break.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


@dataclass(slots=True)
class WhatsAppClient:
    """A thin, honest wrapper over the Graph endpoints TutorTwin actually uses.

    No SDK: three HTTP calls do not justify a dependency, and the Graph API is
    versioned in the URL, so an SDK upgrade is the same work as a string change.
    """

    access_token: str
    phone_number_id: str
    api_version: str = "v21.0"
    timeout_seconds: float = 10.0
    send_enabled: bool = True
    _client: httpx.AsyncClient | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, to: str, payload: dict[str, Any]) -> str | None:
        """POST one message. Returns Meta's message id, or None if disabled.

        Failures are logged and swallowed. A student's answer is already
        persisted by the time this runs, so raising here would fail a request
        that has succeeded, and Cloud Tasks would retry the whole turn - paying
        a second time for a model call whose answer we already hold.
        """
        if not self.send_enabled:
            logger.info("whatsapp_send_disabled", to_suffix=to[-4:], kind=payload.get("type"))
            return None

        url = f"{GRAPH_HOST}/{self.api_version}/{self.phone_number_id}/messages"
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            **payload,
        }
        try:
            response = await self._http().post(url, headers=self._headers, json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "whatsapp_send_failed",
                status=exc.response.status_code,
                # Meta puts the actionable part in the body: an expired token
                # and an unopened 24-hour window are both 400.
                detail=exc.response.text[:500],
                kind=payload.get("type"),
            )
            return None
        except httpx.HTTPError as exc:
            logger.error("whatsapp_send_error", error_type=type(exc).__name__)
            return None

        messages = response.json().get("messages") or [{}]
        message_id = messages[0].get("id")
        logger.info("whatsapp_sent", kind=payload.get("type"), message_id=message_id)
        return str(message_id) if message_id else None

    async def send_text(self, to: str, text: str) -> None:
        for part in _chunk(text):
            # `preview_url` off: a link in a tutoring answer is a citation, and
            # an unfurled preview card pushes the actual answer off screen.
            await self.send(to, {"type": "text", "text": {"preview_url": False, "body": part}})

    async def send_template(
        self,
        to: str,
        *,
        name: str,
        language: str = "en",
        body_params: tuple[str, ...] = (),
    ) -> str | None:
        """Send a pre-approved template.

        This is the **only** way to message somebody who has not written to us
        in the last 24 hours. A subscription confirmation almost always falls
        outside that window - the student paid on a website, they were not
        mid-conversation - so a plain text message there is rejected with a 400
        and the student silently never learns their subscription is live.

        Meta requires template parameters to be positional and to match the
        approved body exactly, so they are passed as an ordered tuple rather
        than a mapping: a named dict would imply a flexibility Meta does not
        have.
        """
        components: list[dict[str, Any]] = []
        if body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                }
            )

        return await self.send(
            to,
            {
                "type": "template",
                "template": {
                    "name": name,
                    "language": {"code": language},
                    **({"components": components} if components else {}),
                },
            },
        )

    async def upload_media(self, data: bytes, mime_type: str, filename: str) -> str | None:
        """Upload bytes to Meta and get a media id back.

        This is what lets a generated graph or worksheet reach the student
        without hosting it anywhere public. The alternative - a signed R2 URL -
        works too, but it puts student coursework on a guessable-if-leaked
        public URL, and it fails entirely on a deployment with no R2.

        Media ids expire after roughly 30 days, which is irrelevant here: it is
        used within seconds of being issued.
        """
        if not self.send_enabled:
            logger.info("whatsapp_upload_disabled", mime_type=mime_type)
            return None

        url = f"{GRAPH_HOST}/{self.api_version}/{self.phone_number_id}/media"
        try:
            response = await self._http().post(
                url,
                headers=self._headers,
                files={"file": (filename, data, mime_type)},
                data={"messaging_product": "whatsapp", "type": mime_type},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "whatsapp_media_upload_failed",
                status=exc.response.status_code,
                detail=exc.response.text[:500],
            )
            return None
        except httpx.HTTPError as exc:
            logger.error("whatsapp_media_upload_error", error_type=type(exc).__name__)
            return None

        media_id = response.json().get("id")
        logger.info("whatsapp_media_uploaded", media_id=media_id, bytes=len(data))
        return str(media_id) if media_id else None

    async def send_media(
        self,
        to: str,
        *,
        data: bytes,
        mime_type: str,
        filename: str,
        kind: str,
        caption: str | None = None,
    ) -> str | None:
        """Upload then send, in one step. `kind` is image, document or audio."""
        media_id = await self.upload_media(data, mime_type, filename)
        if media_id is None:
            return None

        block: dict[str, Any] = {"id": media_id}
        # Meta rejects a caption on a document unless a filename is also given,
        # and shows the raw media id as the name when it is missing.
        if kind == "document":
            block["filename"] = filename
        if caption and kind in ("image", "document", "video"):
            block["caption"] = caption[:1024]

        return await self.send(to, {"type": kind, kind: block})

    async def download(self, media_id: str) -> bytes:
        """Two hops, both authenticated.

        Meta hands out a short-lived lookaside URL rather than the bytes, and
        that URL still requires the bearer token - fetching it unauthenticated
        returns a redirect to an error page, which arrives as valid-looking
        HTML instead of an image.
        """
        client = self._http()
        meta_url = f"{GRAPH_HOST}/{self.api_version}/{media_id}"
        try:
            lookup = await client.get(meta_url, headers=self._headers)
            lookup.raise_for_status()
            url = lookup.json().get("url")
            if not url:
                raise MediaNotFound(media_id)

            blob = await client.get(url, headers=self._headers)
            blob.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error(
                "whatsapp_media_download_failed",
                media_id=media_id,
                error_type=type(exc).__name__,
            )
            raise MediaNotFound(media_id) from exc

        logger.info("whatsapp_media_downloaded", media_id=media_id, bytes=len(blob.content))
        return blob.content


@dataclass(slots=True)
class WhatsAppMediaSource:
    """`MediaSource` over the Graph API."""

    client: WhatsAppClient
    fetches: list[tuple[str, str]] = field(default_factory=list)

    @property
    def fetch_count(self) -> int:
        return len(self.fetches)

    async def fetch(self, provider: str, media_id: str) -> bytes:
        self.fetches.append((provider, media_id))
        return await self.client.download(media_id)


@dataclass(slots=True)
class RoutingMediaSource:
    """Sends each fetch to the source that owns that provider.

    One deployment serves WhatsApp media and locally-staged test files at the
    same time, and `MediaRef` already carries the provider that can resolve it.
    """

    default: MediaSource
    routes: dict[str, MediaSource] = field(default_factory=dict)

    async def fetch(self, provider: str, media_id: str) -> bytes:
        return await self.routes.get(provider, self.default).fetch(provider, media_id)


@dataclass(slots=True)
class WhatsAppOutboundGateway:
    """`OutboundGateway` over the Graph API.

    Every action type reaches the student as something. An action silently
    dropped here is an answer the student paid for and never saw.
    """

    client: WhatsAppClient
    subject_type: str = "whatsapp"

    math_images: bool = True
    """Follow a maths answer with typeset images of its display equations."""

    max_math_images: int = 3
    """Per message. A worked solution can contain a dozen steps, and twelve
    images bury the explanation they were meant to clarify."""

    async def deliver(self, subject: ResolvedSubject, actions: tuple[OutboundAction, ...]) -> None:
        if subject.external_type != self.subject_type:
            # An admin-console or test subject has no phone number. Not an
            # error, just not ours to deliver.
            logger.info("whatsapp_skip_non_whatsapp_subject", external_type=subject.external_type)
            return

        to = subject.external_id
        for action in actions:
            text = (action.text or "").strip()
            if not text:
                continue

            link = _MEDIA_ACTIONS.get(action.type)
            if link and text.startswith(("http://", "https://")):
                await self.client.send(to, {"type": link, link: {"link": text}})
                continue

            # SHOW_UPGRADE, SHOW_MENU, ASK_FILE_BRIEF and TUTOR_NOTIFICATION are
            # all prose today. Interactive buttons are a presentation upgrade,
            # not a different message, so they can arrive later without any
            # change to orchestration.
            await self.client.send_text(to, text)
            await self._send_math_images(to, text)

    async def _send_math_images(self, to: str, text: str) -> None:
        """Follow a maths answer with the equations, typeset.

        WhatsApp renders no mathematics, so `x = (-b +- sqrt(b^2-4ac))/2a`
        arrives as exactly the ambiguous string students misread. Sending a
        typeset image after the text is the largest legibility win available on
        this channel, and it is additive: the text is already delivered, so a
        render or upload failure costs nothing.

        Rendered here rather than in orchestration because it is a *presentation*
        decision belonging to this channel. A web client would typeset the same
        LaTeX itself and want no image at all.
        """
        if not self.math_images:
            return

        # Import here: orchestration and the webhook both import this module,
        # and matplotlib is a heavy import to pay for on every code path.
        from tutortwin.learning import mathrender

        for expression in mathrender.extract_display_math(text)[: self.max_math_images]:
            try:
                rendered = mathrender.render(expression)
            except mathrender.UnrenderableMath:
                # Expected, not exceptional: the text already went out.
                continue
            await self.client.send_media(
                to,
                data=rendered.png,
                mime_type="image/png",
                filename="equation.png",
                kind="image",
            )


__all__ = [
    "GRAPH_HOST",
    "TEXT_LIMIT",
    "RoutingMediaSource",
    "WhatsAppClient",
    "WhatsAppMediaSource",
    "WhatsAppOutboundGateway",
]
