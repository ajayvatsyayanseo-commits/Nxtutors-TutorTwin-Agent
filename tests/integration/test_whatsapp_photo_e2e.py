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
