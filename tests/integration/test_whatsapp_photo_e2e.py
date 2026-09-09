"""A student photographs a homework page and sends it. The headline path.

This is the one journey the whole product is judged on, and it crosses every
component at once: Meta's webhook, signature verification, the brief gate, the
job queue, media validation, storage, and OCR.

Only two things are stubbed, both because a test must not reach them: the Graph
API (which would download from Meta and message a real phone) and the vision
model (which would cost money). **OCR is real.** If Tesseract is not installed
the OCR assertions skip rather than pass quietly - a green run that proved
nothing is worse than a skip that says so.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from tutortwin.api.app import create_app
from tutortwin.api.dependencies import build_container, set_container
from tutortwin.config import Settings
from tutortwin.db.engine import dispose_engine, get_session_factory
from tutortwin.db.models import Job, MediaObject
from tutortwin.domain.media import MediaState
from tutortwin.integrations.whatsapp.client import WhatsAppOutboundGateway
from tutortwin.media.ocr import TesseractOCRProvider
from tutortwin.orchestration.entry_service import EntryDependencies, TutorTwinEntryService
from tutortwin.providers.fakes import (
    FakeEntitlementGateway,
    FakeIdentityGateway,
    FakeTutorGateway,
    SystemClock,
)

from ..conftest import TEST_DSN

pytestmark = pytest.mark.integration

APP_SECRET = "photo-app-secret"
STUDENT = "919999000777"
MEDIA_ID = "META_MEDIA_9001"

QUESTION_LINES = (
    "Question 3.",
    "Name the three parts of a plant cell",
    "that are absent in an animal cell.",
)


def homework_photo() -> bytes:
    """A JPEG, the format WhatsApp actually delivers."""
    image = Image.new("RGB", (1000, 420), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except OSError:  # pragma: no cover - depends on the host's fonts
        font = ImageFont.load_default()
    for index, line in enumerate(QUESTION_LINES):
        draw.text((40, 60 + index * 80), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


class StubGraph:
    """Meta, stubbed at both ends: no downloads, no messages to real phones."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[str] = []

    async def send(self, to: str, payload: dict[str, Any]) -> str | None:
        self.sent.append((to, payload))
        return "wamid.out"

    async def send_text(self, to: str, text: str) -> None:
        await self.send(to, {"type": "text", "text": {"body": text}})

    async def download(self, media_id: str) -> bytes:
        self.downloads.append(media_id)
        return homework_photo()

    @property
    def texts(self) -> list[str]:
        return [p["text"]["body"] for _, p in self.sent if p.get("type") == "text"]


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def photo_payload(*, wamid: str, caption: str | None) -> bytes:
    image: dict[str, Any] = {
        "id": MEDIA_ID,
        "mime_type": "image/jpeg",
        "sha256": "irrelevant-here",
    }
    if caption is not None:
        image["caption"] = caption
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "PNID"},
                                "contacts": [{"profile": {"name": "Aarav"}, "wa_id": STUDENT}],
                                "messages": [
                                    {
                                        "from": STUDENT,
                                        "id": wamid,
                                        "timestamp": "1767225600",
                                        "type": "image",
                                        "image": image,
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def text_payload(text: str, *, wamid: str) -> bytes:
    """A plain text message from the same student - the shape a brief arrives in."""
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "PNID"},
                                "contacts": [{"profile": {"name": "Aarav"}, "wa_id": STUDENT}],
                                "messages": [
                                    {
                                        "from": STUDENT,
                                        "id": wamid,
                                        "timestamp": "1767225700",
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


@pytest_asyncio.fixture
async def wired(session: object, tmp_path: Any) -> AsyncIterator[tuple[AsyncClient, StubGraph]]:
    await dispose_engine()
    settings = Settings(
        environment="test",
        database_url=TEST_DSN,  # type: ignore[arg-type]
        internal_api_key="test-internal-key",  # type: ignore[arg-type]
        media_root=str(tmp_path),
        whatsapp_access_token="graph-token",  # type: ignore[arg-type]
        whatsapp_phone_number_id="PNID",
        whatsapp_app_secret=APP_SECRET,  # type: ignore[arg-type]
        whatsapp_verify_token="vt",  # type: ignore[arg-type]
    )
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        graph = StubGraph()
        container = build_container(settings)

        # Swap the Graph client out from under the already-built adapters, so
        # the real WhatsAppMediaSource and WhatsAppOutboundGateway are still the
        # code under test - only the network beneath them is replaced.
        container.media_pipeline._source.routes["whatsapp"].client = graph  # type: ignore[union-attr]
        outbound = WhatsAppOutboundGateway(client=graph)  # type: ignore[arg-type]
        identity = FakeIdentityGateway()
        container.outbound = outbound
        container.identity = identity
        container.entry_service = TutorTwinEntryService(
            EntryDependencies(
                identity=identity,
                entitlement=FakeEntitlementGateway(plans={STUDENT: "PRO"}),
                tutor=FakeTutorGateway(),
                outbound=outbound,
                clock=SystemClock(),
                session_factory=get_session_factory(),
                gateway_factory=None,
                media_pipeline=container.media_pipeline,
            )
        )
        set_container(app, container)
        yield client, graph
    await dispose_engine()


async def post_photo(client: AsyncClient, *, wamid: str, caption: str | None) -> Any:
    body = photo_payload(wamid=wamid, caption=caption)
    return await client.post(
        "/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sign(body)}
    )


async def test_a_photo_with_no_caption_is_held_and_costs_nothing(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """The brief gate. A bare photo is ambiguous - solve it? explain it? check
    my working? - and guessing wrong means paying a vision model to answer a
    question nobody asked.
    """
    client, graph = wired

    response = await post_photo(client, wamid="wamid.photo.nobrief", caption=None)

    assert response.json()["handled"] == 1
    assert graph.downloads == [], "the file must not be downloaded before it is briefed"
    assert graph.texts, "the student must be told what is needed"

    async with get_session_factory()() as db:
        media = (await db.execute(select(MediaObject))).scalars().one()
        assert media.state == MediaState.WAITING_FOR_BRIEF.value
        assert (await db.execute(select(Job))).scalars().all() == []


async def test_a_captioned_photo_queues_work_and_acknowledges(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """A caption is a brief, so the same photo may now be read."""
    client, graph = wired

    response = await post_photo(
        client, wamid="wamid.photo.brief", caption="solve question 3 please"
    )

    assert response.json()["handled"] == 1
    async with get_session_factory()() as db:
        media = (await db.execute(select(MediaObject))).scalars().one()
        assert media.brief == "solve question 3 please"
        job = (await db.execute(select(Job))).scalars().one()
        assert job.job_type == "MEDIA_EXTRACT"
        assert job.media_object_id == media.id

    # The student is told the file is being read, rather than left in silence.
    assert graph.texts and any("read" in t.lower() for t in graph.texts)


async def test_the_queued_job_downloads_and_reads_the_photo(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """The end of the journey: real OCR turns the student's photo into text the
    tutor can answer from.

    Before the image extraction path existed, this test's final assertion was
    the one that failed - the photo was fetched, validated, stored and marked
    ready with no text extracted from it at all.
    """
    client, graph = wired
    await post_photo(client, wamid="wamid.photo.run", caption="what is question 3 asking")

    async with get_session_factory()() as db:
        job = (await db.execute(select(Job))).scalars().one()

    response = await client.post(
        "/internal/jobs/run",
        json={"job_id": str(job.id)},
        headers={"x-internal-key": "test-internal-key"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "SUCCEEDED"
    assert graph.downloads == [MEDIA_ID], "the photo must be fetched from the Graph API"

    async with get_session_factory()() as db:
        media = (await db.execute(select(MediaObject))).scalars().one()
        assert media.state == MediaState.READY_FOR_CAPABILITY.value
        assert media.sha256, "the bytes were stored and content-addressed"
        assert media.mime_type == "image/jpeg"

    if not TesseractOCRProvider(command=None).available:
        pytest.skip("Tesseract is not installed; the OCR assertion cannot run honestly")

    from tutortwin.db.models import MediaExtraction

    async with get_session_factory()() as db:
        rows = (await db.execute(select(MediaExtraction))).scalars().all()
        assert rows, "the photo produced no extraction at all"
        text = " ".join(r.text for r in rows).lower()

    # Not an exact match: OCR is never exact, and asserting one would make this
    # test fail on a font change rather than on a real regression.
    assert "plant cell" in text, f"OCR did not read the question: {text!r}"
    assert "question 3" in text


async def test_a_brief_sent_as_a_later_message_reaches_the_waiting_photo(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """The brief gate has to be a conversation, not a dead end.

    The agent asks "tell me what you would like me to do with it". The student's
    reply arrives as an ordinary TEXT message. Without a path from that text
    back to the held MediaObject, the model answers "solve question 3" with no
    image attached and the photo waits forever - the agent asks a question and
    ignores the answer.
    """
    client, graph = wired

    # 1. A photo with no caption. Held, nothing downloaded.
    await post_photo(client, wamid="wamid.brief.photo", caption=None)
    async with get_session_factory()() as db:
        media = (await db.execute(select(MediaObject))).scalars().one()
        assert media.state == MediaState.WAITING_FOR_BRIEF.value
        assert graph.downloads == []
        assert (await db.execute(select(Job))).scalars().all() == []

    # 2. The student answers the question the agent asked.
    body = text_payload("solve question 3 please", wamid="wamid.brief.text")
    response = await client.post(
        "/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sign(body)}
    )
    assert response.json()["handled"] == 1

    # 3. That text became the brief, and the work is now queued.
    async with get_session_factory()() as db:
        media = (await db.execute(select(MediaObject))).scalars().one()
        assert media.brief == "solve question 3 please", (
            "the student's reply did not reach the waiting photo"
        )
        assert media.state != MediaState.WAITING_FOR_BRIEF.value
        job = (await db.execute(select(Job))).scalars().one()
        assert job.media_object_id == media.id

    assert any("read" in t.lower() for t in graph.texts)


async def test_the_next_message_becomes_the_brief_for_the_held_photo(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """The agent asked what to do with the file, so the reply is the answer.

    This used to assert only that two STUDENT turns existed, which is true
    whether the reply was used as a brief or silently dropped - so it could not
    fail. What actually has to hold is that the photo LEAVES the waiting state
    and a job is created, because the alternative is the dead end this path was
    built to close: the agent asks a question and then ignores the answer.
    """
    client, _ = wired
    await post_photo(client, wamid="wamid.resume.photo", caption=None)

    async with get_session_factory()() as db:
        held = (await db.execute(select(MediaObject))).scalars().one()
        assert held.state == MediaState.WAITING_FOR_BRIEF.value
        # Recorded at intake, before any download, so the resumed brief can
        # charge the right daily ceiling instead of defaulting to none.
        assert held.kind == "IMAGE"

    body = text_payload("solve question 3 please", wamid="wamid.resume.text")
    await client.post(
        "/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sign(body)}
    )

    async with get_session_factory()() as db:
        media = (await db.execute(select(MediaObject))).scalars().one()
        assert media.state != MediaState.WAITING_FOR_BRIEF.value
        assert media.brief == "solve question 3 please"
        job = (await db.execute(select(Job))).scalars().one()
        assert job.media_object_id == media.id


async def test_a_photo_abandoned_yesterday_does_not_swallow_todays_question(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """The bound on the resume path, and the reason it exists.

    A photo left in WAITING_FOR_BRIEF used to sit there forever, claiming
    whatever the student typed next - a question asked a week later would be
    answered as an instruction about a picture they had forgotten sending. The
    window is the same 24 hours as WhatsApp's own customer service window,
    after which the conversation is closed and the next message starts a new one.
    """
    client, _ = wired
    await post_photo(client, wamid="wamid.stale.photo", caption=None)

    async with get_session_factory()() as db:
        held = (await db.execute(select(MediaObject))).scalars().one()
        await db.execute(
            MediaObject.__table__.update()
            .where(MediaObject.id == held.id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )
        await db.commit()

    body = text_payload("what is photosynthesis", wamid="wamid.stale.text")
    await client.post(
        "/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sign(body)}
    )

    async with get_session_factory()() as db:
        media = (await db.execute(select(MediaObject))).scalars().one()
        assert media.state == MediaState.WAITING_FOR_BRIEF.value, (
            "a day-old photo must not claim this message"
        )
        assert media.brief is None
        assert (await db.execute(select(Job))).scalars().all() == []

        from tutortwin.db.models import Message

        roles = [m.role for m in (await db.execute(select(Message))).scalars().all()]
        assert roles.count("STUDENT") >= 2, "the question was not recorded as its own turn"
