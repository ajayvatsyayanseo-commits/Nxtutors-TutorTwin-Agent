"""The Meta boundary.

Everything here is untrusted input arriving at a public URL, so the tests are
written the way an attacker reads the code: what gets through, and what a
student loses when the parser is careless.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest

from tutortwin.domain.events import MessageType, OutboundAction, OutboundActionType
from tutortwin.domain.models import ResolvedSubject, SubjectStatus
from tutortwin.integrations.whatsapp.client import (
    SEND_ATTEMPTS,
    RoutingMediaSource,
    WhatsAppClient,
    WhatsAppMediaSource,
    WhatsAppOutboundGateway,
    _chunk,
)
from tutortwin.integrations.whatsapp.webhook import (
    SignatureError,
    normalize,
    verify_challenge,
    verify_signature,
)
from tutortwin.providers.fakes import deterministic_uuid

SECRET = "app-secret-value"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def envelope(*messages: dict[str, Any], field: str = "messages") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": field,
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "PNID"},
                            "contacts": [{"wa_id": "919999000001", "profile": {"name": "Aarav"}}],
                            "messages": list(messages),
                        },
                    }
                ],
            }
        ],
    }


def text_message(**over: Any) -> dict[str, Any]:
    base = {
        "from": "919999000001",
        "id": "wamid.HBgMOTE=",
        "timestamp": "1767225600",
        "type": "text",
        "text": {"body": "Explain photosynthesis"},
    }
    base.update(over)
    return base


class TestSignature:
    def test_a_genuine_payload_passes(self) -> None:
        body = b'{"entry":[]}'
        verify_signature(body=body, header=sign(body), app_secret=SECRET)

    def test_a_tampered_body_is_refused(self) -> None:
        """The whole point: the signature covers the bytes, so changing one
        character of the student's message invalidates it."""
        header = sign(b'{"amount":10}')
        with pytest.raises(SignatureError, match="mismatch"):
            verify_signature(body=b'{"amount":9999}', header=header, app_secret=SECRET)

    def test_the_wrong_secret_is_refused(self) -> None:
        body = b'{"entry":[]}'
        with pytest.raises(SignatureError, match="mismatch"):
            verify_signature(body=body, header=sign(body, "someone-elses"), app_secret=SECRET)

    @pytest.mark.parametrize("header", [None, "", "deadbeef", "sha1=deadbeef", "sha256="])
    def test_a_missing_or_malformed_header_is_refused(self, header: str | None) -> None:
        with pytest.raises(SignatureError):
            verify_signature(body=b"{}", header=header, app_secret=SECRET)

    def test_an_unconfigured_secret_refuses_everything(self) -> None:
        """Fail closed. An empty secret must not become a wildcard that accepts
        any payload, which is what a bare HMAC comparison would do."""
        body = b"{}"
        with pytest.raises(SignatureError, match="no app secret"):
            verify_signature(body=body, header=sign(body, ""), app_secret="")


class TestChallenge:
    def test_the_challenge_is_echoed(self) -> None:
        assert (
            verify_challenge(mode="subscribe", token="vt", challenge="12345", verify_token="vt")
            == "12345"
        )

    @pytest.mark.parametrize(
        ("mode", "token", "challenge"),
        [
            ("subscribe", "wrong", "12345"),
            ("unsubscribe", "vt", "12345"),
            ("subscribe", "vt", ""),
            ("subscribe", None, "12345"),
        ],
    )
    def test_anything_else_is_refused(self, mode: str, token: str | None, challenge: str) -> None:
        with pytest.raises(SignatureError):
            verify_challenge(mode=mode, token=token, challenge=challenge, verify_token="vt")


class TestNormalize:
    def test_a_text_message_becomes_one_event(self) -> None:
        events = normalize(envelope(text_message()))
        assert len(events) == 1
        event = events[0]
        assert event.message.type is MessageType.TEXT
        assert event.message.text == "Explain photosynthesis"
        assert event.subject.external_type == "whatsapp"
        assert event.subject.external_id == "919999000001"
        assert event.source == "whatsapp"

    def test_the_wamid_is_the_idempotency_key(self) -> None:
        """Meta redelivers on any non-2xx and on its own timeouts. If a retry
        did not collide with the original, every hiccup would buy a second
        model call and send the student the same answer twice.
        """
        events = normalize(envelope(text_message()))
        assert events[0].idempotency_key == "whatsapp:wamid.HBgMOTE="

    def test_delivery_receipts_produce_nothing(self) -> None:
        """The single most common callback. A conversation turn per read
        receipt would bury the real messages and cost money doing it."""
        statuses = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "statuses": [{"id": "wamid.X", "status": "read"}],
                            },
                        }
                    ],
                }
            ],
        }
        assert normalize(statuses) == []

    def test_a_photo_carries_its_caption(self) -> None:
        """The caption is the question. Lose it and the brief gate asks the
        student for something they already sent."""
        events = normalize(
            envelope(
                text_message(
                    type="image",
                    text=None,
                    image={
                        "id": "MEDIA_1",
                        "mime_type": "image/jpeg",
                        "file_size": 90_000,
                        "caption": "solve only Q3",
                    },
                )
            )
        )
        assert len(events) == 1
        message = events[0].message
        assert message.type is MessageType.IMAGE
        assert message.text == "solve only Q3"
        assert message.media is not None
        assert message.media.provider == "whatsapp"
        assert message.media.media_id == "MEDIA_1"
        assert message.media.mime_type_hint == "image/jpeg"
        assert message.media.size_hint == 90_000

    def test_a_photo_with_no_caption_is_still_an_event(self) -> None:
        events = normalize(
            envelope(
                text_message(type="image", text=None, image={"id": "M", "mime_type": "image/jpeg"})
            )
        )
        assert len(events) == 1
        assert events[0].message.text is None
        assert events[0].message.media is not None

    def test_a_pdf_is_typed_as_pdf_not_document(self) -> None:
        events = normalize(
            envelope(
                text_message(
                    type="document",
                    text=None,
                    document={
                        "id": "DOC",
                        "mime_type": "application/pdf",
                        "filename": "worksheet.pdf",
                    },
                )
            )
        )
        assert events[0].message.type is MessageType.PDF
        assert events[0].message.media is not None
        assert events[0].message.media.filename == "worksheet.pdf"

    def test_a_voice_note_is_audio(self) -> None:
        events = normalize(
            envelope(
                text_message(type="audio", text=None, audio={"id": "A", "mime_type": "audio/ogg"})
            )
        )
        assert events[0].message.type is MessageType.AUDIO

    def test_a_button_reply_becomes_an_action(self) -> None:
        events = normalize(
            envelope(
                text_message(
                    type="interactive",
                    text=None,
                    interactive={
                        "type": "button_reply",
                        "button_reply": {"id": "menu_practice", "title": "Practice"},
                    },
                )
            )
        )
        assert events[0].message.type is MessageType.ACTION
        assert events[0].message.text == "Practice"

    def test_a_sticker_is_ignored_rather_than_answered(self) -> None:
        events = normalize(envelope(text_message(type="sticker", text=None, sticker={"id": "S"})))
        assert events == []

    def test_one_bad_message_does_not_discard_the_batch(self) -> None:
        """A batch can carry several students. Rejecting the whole envelope
        because of one malformed entry silently drops the others."""
        events = normalize(
            envelope(
                {"type": "text", "text": {"body": "no id, no sender"}},
                text_message(id="wamid.ok"),
            )
        )
        assert [e.message.message_id for e in events] == ["wamid.ok"]

    def test_a_non_message_field_is_skipped(self) -> None:
        assert normalize(envelope(text_message(), field="account_update")) == []

    def test_an_empty_envelope_is_not_an_error(self) -> None:
        assert normalize({}) == []


class TestChunking:
    def test_short_text_is_one_message(self) -> None:
        assert _chunk("hello") == ["hello"]

    def test_nothing_is_lost_when_splitting(self) -> None:
        """The student must receive the whole derivation, not the first 4096
        characters of it."""
        text = "\n\n".join(f"Step {i}: " + "x" * 200 for i in range(60))
        parts = _chunk(text)
        assert len(parts) > 1
        # Compared with every whitespace run removed: the split is allowed to
        # move a line break, never to drop a character of the derivation.
        assert "".join("".join(p.split()) for p in parts) == "".join(text.split())

    def test_every_part_fits_metas_limit(self) -> None:
        parts = _chunk("y" * 20_000)
        assert parts and all(len(p) <= 4096 for p in parts)

    def test_a_break_prefers_a_paragraph_boundary(self) -> None:
        text = ("a" * 4000) + "\n\n" + ("b" * 4000)
        parts = _chunk(text)
        assert parts[0] == "a" * 4000


class _StubClient:
    """Records sends instead of calling Meta."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send(self, to: str, payload: dict[str, Any]) -> str | None:
        self.sent.append((to, payload))
        return "wamid.out"

    async def send_text(self, to: str, text: str) -> None:
        for part in _chunk(text):
            await self.send(to, {"type": "text", "text": {"body": part}})


def subject(external_type: str = "whatsapp", external_id: str = "919999000001") -> ResolvedSubject:
    return ResolvedSubject(
        id=deterministic_uuid("subject", external_type, external_id),
        external_type=external_type,
        external_id=external_id,
        display_name=None,
        status=SubjectStatus.ACTIVE,
    )


class TestOutbound:
    @pytest.mark.asyncio
    async def test_an_answer_reaches_the_students_number(self) -> None:
        stub = _StubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]

        await gateway.deliver(
            subject(),
            (OutboundAction(type=OutboundActionType.SEND_TEXT, text="The answer is 42"),),
        )

        assert stub.sent == [
            ("919999000001", {"type": "text", "text": {"body": "The answer is 42"}})
        ]

    @pytest.mark.asyncio
    async def test_every_action_type_reaches_the_student(self) -> None:
        """A dropped action is an answer the student paid for and never saw."""
        stub = _StubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]

        await gateway.deliver(
            subject(),
            tuple(OutboundAction(type=t, text=f"text for {t}") for t in OutboundActionType),
        )

        assert len(stub.sent) == len(OutboundActionType)

    @pytest.mark.asyncio
    async def test_a_document_url_is_sent_as_a_document(self) -> None:
        stub = _StubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]

        await gateway.deliver(
            subject(),
            (
                OutboundAction(
                    type=OutboundActionType.SEND_DOCUMENT,
                    text="https://cdn.example.com/worksheet.pdf",
                ),
            ),
        )

        assert stub.sent[0][1] == {
            "type": "document",
            "document": {"link": "https://cdn.example.com/worksheet.pdf"},
        }

    @pytest.mark.asyncio
    async def test_a_document_action_without_a_url_still_says_something(self) -> None:
        stub = _StubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]

        await gateway.deliver(
            subject(),
            (OutboundAction(type=OutboundActionType.SEND_DOCUMENT, text="Worksheet ready"),),
        )

        assert stub.sent[0][1]["type"] == "text"

    @pytest.mark.asyncio
    async def test_a_non_whatsapp_subject_is_skipped_not_misdelivered(self) -> None:
        """An admin-console subject has no phone number. Sending its external id
        to Meta would either error or, worse, reach an unrelated number."""
        stub = _StubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]

        await gateway.deliver(
            subject(external_type="admin", external_id="admin@example.com"),
            (OutboundAction(type=OutboundActionType.SEND_TEXT, text="hi"),),
        )

        assert stub.sent == []

    @pytest.mark.asyncio
    async def test_an_empty_action_sends_nothing(self) -> None:
        stub = _StubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]
        await gateway.deliver(
            subject(), (OutboundAction(type=OutboundActionType.SEND_TEXT, text="   "),)
        )
        assert stub.sent == []


def _transport(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestGraphClient:
    @pytest.mark.asyncio
    async def test_a_send_carries_the_bearer_token_and_the_right_shape(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"messages": [{"id": "wamid.sent"}]})

        client = WhatsAppClient(access_token="TOKEN", phone_number_id="PNID")
        client._client = _transport(handler)

        await client.send_text("919999000001", "hello")

        assert seen["auth"] == "Bearer TOKEN"
        assert seen["url"].endswith("/v21.0/PNID/messages")
        assert seen["body"]["messaging_product"] == "whatsapp"
        assert seen["body"]["to"] == "919999000001"
        assert seen["body"]["text"]["body"] == "hello"

    @pytest.mark.asyncio
    async def test_the_kill_switch_stops_every_send(self) -> None:
        """The first time this points at a production number, nothing should
        reach a real student until someone flips the switch on purpose."""

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("a disabled sender must not call Meta")

        client = WhatsAppClient(access_token="T", phone_number_id="P", send_enabled=False)
        client._client = _transport(handler)

        assert await client.send("919999000001", {"type": "text"}) is None

    @pytest.mark.asyncio
    async def test_a_meta_error_does_not_raise(self) -> None:
        """The answer is already persisted and already billed. Raising here
        would make Cloud Tasks retry the whole turn and pay a second time."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": "24h window closed"}})

        client = WhatsAppClient(access_token="T", phone_number_id="P")
        client._client = _transport(handler)

        assert await client.send("91999", {"type": "text"}) is None

    @pytest.mark.asyncio
    async def test_media_download_is_two_authenticated_hops(self) -> None:
        """Meta returns a lookaside URL, not bytes, and that URL still needs the
        token - fetch it unauthenticated and an HTML error page arrives dressed
        as an image."""
        calls: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((str(request.url), request.headers.get("authorization")))
            if request.url.path.endswith("/MEDIA_1"):
                return httpx.Response(
                    200, json={"url": "https://lookaside.fbsbx.com/x", "mime_type": "image/jpeg"}
                )
            return httpx.Response(200, content=b"\xff\xd8\xff-jpeg-bytes")

        client = WhatsAppClient(access_token="TOKEN", phone_number_id="P")
        client._client = _transport(handler)

        assert await client.download("MEDIA_1") == b"\xff\xd8\xff-jpeg-bytes"
        assert len(calls) == 2
        assert all(auth == "Bearer TOKEN" for _, auth in calls)

    @pytest.mark.asyncio
    async def test_a_failed_download_is_media_not_found(self) -> None:
        from tutortwin.media.adapters import MediaNotFound

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"message": "expired"}})

        client = WhatsAppClient(access_token="T", phone_number_id="P")
        client._client = _transport(handler)

        with pytest.raises(MediaNotFound):
            await client.download("GONE")


class TestMediaRouting:
    @pytest.mark.asyncio
    async def test_whatsapp_media_goes_to_the_graph_api(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/M"):
                return httpx.Response(200, json={"url": "https://lookaside/x"})
            return httpx.Response(200, content=b"photo")

        client = WhatsAppClient(access_token="T", phone_number_id="P")
        client._client = _transport(handler)

        class _Local:
            async def fetch(self, provider: str, media_id: str) -> bytes:  # pragma: no cover
                raise AssertionError("whatsapp media must not be read from disk")

        source = RoutingMediaSource(
            default=_Local(),  # type: ignore[arg-type]
            routes={"whatsapp": WhatsAppMediaSource(client=client)},
        )

        assert await source.fetch("whatsapp", "M") == b"photo"

    @pytest.mark.asyncio
    async def test_other_providers_keep_the_default_source(self) -> None:
        class _Local:
            async def fetch(self, provider: str, media_id: str) -> bytes:
                return b"from disk"

        source = RoutingMediaSource(default=_Local(), routes={})  # type: ignore[arg-type]
        assert await source.fetch("local", "f.png") == b"from disk"


class _MathStubClient(_StubClient):
    """Records media sends as well as text, so a maths answer can be asserted."""

    def __init__(self) -> None:
        super().__init__()
        self.media: list[tuple[str, str, int]] = []

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
        self.media.append((to, kind, len(data)))
        return "wamid.media"


class TestMathImages:
    """WhatsApp renders no mathematics.

    A quadratic formula sent as text arrives as `x = (-b +- sqrt(b^2-4ac))/2a`,
    which is exactly the string students misread. The answer text still goes
    first - the image is additive, so a render failure costs nothing.
    """

    @pytest.mark.asyncio
    async def test_display_maths_is_also_sent_as_an_image(self) -> None:
        stub = _MathStubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]

        await gateway.deliver(
            subject(),
            (
                OutboundAction(
                    type=OutboundActionType.SEND_TEXT,
                    # Raw: LaTeX is mostly backslashes, and \f \p \s are not
                    # valid Python escapes.
                    text="Rearrange, then apply:\n\n" + r"$$x = \frac{-b \pm \sqrt{b^2-4ac}}{2a}$$",
                ),
            ),
        )

        assert stub.sent, "the answer text must still be sent first"
        assert len(stub.media) == 1
        to, kind, size = stub.media[0]
        assert to == "919999000001"
        assert kind == "image"
        assert size > 500, "a real PNG, not an empty render"

    @pytest.mark.asyncio
    async def test_prose_with_no_maths_sends_no_images(self) -> None:
        stub = _MathStubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]

        await gateway.deliver(
            subject(),
            (
                OutboundAction(
                    type=OutboundActionType.SEND_TEXT,
                    text="Photosynthesis happens in the chloroplast.",
                ),
            ),
        )

        assert stub.sent and stub.media == []

    @pytest.mark.asyncio
    async def test_inline_maths_does_not_trigger_an_image(self) -> None:
        """`$x$` inside a sentence reads fine as text. Rendering every one would
        bury the explanation under a dozen images."""
        stub = _MathStubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]

        await gateway.deliver(
            subject(),
            (
                OutboundAction(
                    type=OutboundActionType.SEND_TEXT,
                    text="Here $x$ is the unknown and $y$ is the constant.",
                ),
            ),
        )

        assert stub.media == []

    @pytest.mark.asyncio
    async def test_the_number_of_images_is_bounded(self) -> None:
        """A worked solution can have a dozen steps. Twelve images bury the
        explanation they were meant to clarify."""
        stub = _MathStubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]
        steps = "\n\n".join(f"$$x_{{{i}}} = {i} + 1$$" for i in range(10))

        await gateway.deliver(
            subject(), (OutboundAction(type=OutboundActionType.SEND_TEXT, text=steps),)
        )

        assert len(stub.media) == gateway.max_math_images

    @pytest.mark.asyncio
    async def test_unrenderable_maths_still_delivers_the_answer(self) -> None:
        """The text is already out. A render failure must never cost the answer."""
        stub = _MathStubClient()
        gateway = WhatsAppOutboundGateway(client=stub)  # type: ignore[arg-type]

        await gateway.deliver(
            subject(),
            (
                OutboundAction(
                    type=OutboundActionType.SEND_TEXT,
                    text="See:\n\n" + r"$$\input{/etc/passwd}$$",
                ),
            ),
        )

        assert stub.sent, "the answer must still reach the student"
        assert stub.media == []

    @pytest.mark.asyncio
    async def test_images_can_be_turned_off(self) -> None:
        stub = _MathStubClient()
        gateway = WhatsAppOutboundGateway(client=stub, math_images=False)  # type: ignore[arg-type]

        await gateway.deliver(
            subject(),
            (OutboundAction(type=OutboundActionType.SEND_TEXT, text="$$x = 1$$"),),
        )

        assert stub.sent and stub.media == []


class TestSendRetry:
    """A transient failure must not cost the student their answer.

    The answer is persisted and the idempotency key is settled before the send
    runs, so Meta's own redelivery replays the stored response WITHOUT
    re-delivering it. One swallowed 429 is therefore a permanent loss of a reply
    the student paid for.
    """

    @pytest.mark.asyncio
    async def test_a_rate_limit_is_retried_and_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            return httpx.Response(200, json={"messages": [{"id": "wamid.ok"}]})

        client = WhatsAppClient(access_token="T", phone_number_id="P")
        client._client = _transport(handler)
        result = await client.send("919999000001", {"type": "text"})

        assert result == "wamid.ok"
        assert calls["n"] == 2, "the 429 was not retried"

    @pytest.mark.asyncio
    async def test_a_server_error_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json={"messages": [{"id": "wamid.late"}]})

        client = WhatsAppClient(access_token="T", phone_number_id="P")
        client._client = _transport(handler)

        assert await client.send("919999000001", {"type": "text"}) == "wamid.late"
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_a_closed_window_is_not_retried(self) -> None:
        """A 400 means the 24-hour window is shut or the token expired. Neither
        changes in two seconds, so retrying only delays the log line that
        explains the problem."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, json={"error": {"message": "24h window closed"}})

        client = WhatsAppClient(access_token="T", phone_number_id="P")
        client._client = _transport(handler)

        assert await client.send("919999000001", {"type": "text"}) is None
        assert calls["n"] == 1, "a permanent failure must not be retried"

    @pytest.mark.asyncio
    async def test_attempts_are_bounded(self) -> None:
        """A student is waiting. Retrying forever is worse than failing."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, text="unavailable")

        client = WhatsAppClient(access_token="T", phone_number_id="P")
        client._client = _transport(handler)

        assert await client.send("919999000001", {"type": "text"}) is None
        assert calls["n"] == SEND_ATTEMPTS

    @pytest.mark.asyncio
    async def test_a_network_error_is_retried_then_gives_up(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectTimeout("no route")

        client = WhatsAppClient(access_token="T", phone_number_id="P")
        client._client = _transport(handler)

        assert await client.send("919999000001", {"type": "text"}) is None
        assert calls["n"] == SEND_ATTEMPTS

    @pytest.mark.asyncio
    async def test_a_failure_never_raises(self) -> None:
        """Raising would fail a request that already succeeded and make the
        queue retry the whole turn - paying twice for one answer."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("connection reset")

        client = WhatsAppClient(access_token="T", phone_number_id="P")
        client._client = _transport(handler)

        assert await client.send("91999", {"type": "text"}) is None
