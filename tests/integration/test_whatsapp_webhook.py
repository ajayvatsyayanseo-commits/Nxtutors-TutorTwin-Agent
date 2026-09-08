"""The webhook, end to end, against a real database.

A Meta payload arrives at the public URL and a student gets an answer. Every
step in between - signature, normalization, identity, entitlement, persistence,
delivery - runs for real; only the Graph API itself is stubbed, because these
tests must not send messages to actual phones.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tutortwin.api.app import create_app
from tutortwin.api.dependencies import build_container, set_container
from tutortwin.config import Settings
from tutortwin.db.engine import dispose_engine, get_session_factory
from tutortwin.db.models import Conversation, Message
from tutortwin.integrations.whatsapp.client import WhatsAppOutboundGateway
from tutortwin.orchestration.entry_service import EntryDependencies, TutorTwinEntryService
from tutortwin.providers.fakes import (
    FakeEntitlementGateway,
    FakeIdentityGateway,
    FakeTutorGateway,
    SystemClock,
)

from ..conftest import TEST_DSN

pytestmark = pytest.mark.integration

APP_SECRET = "webhook-app-secret"
VERIFY_TOKEN = "webhook-verify-token"
STUDENT = "919999000123"


class StubGraph:
    """Stands in for Meta. Records what a student would have received."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send(self, to: str, payload: dict[str, Any]) -> str | None:
        self.sent.append((to, payload))
        return "wamid.out"

    async def send_text(self, to: str, text: str) -> None:
        await self.send(to, {"type": "text", "text": {"body": text}})

    @property
    def texts(self) -> list[str]:
        return [p["text"]["body"] for _, p in self.sent if p.get("type") == "text"]


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def meta_text_payload(text: str, *, wamid: str, sender: str = STUDENT) -> bytes:
    """Byte-for-byte what Meta POSTs for a plain text message."""
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
                                "metadata": {
                                    "display_phone_number": "15550001111",
                                    "phone_number_id": "PNID",
                                },
                                "contacts": [{"profile": {"name": "Aarav"}, "wa_id": sender}],
                                "messages": [
                                    {
                                        "from": sender,
                                        "id": wamid,
                                        "timestamp": "1767225600",
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
async def wired(session: object) -> AsyncIterator[tuple[AsyncClient, StubGraph]]:
    await dispose_engine()
    settings = Settings(
        environment="test",
        database_url=TEST_DSN,  # type: ignore[arg-type]
        internal_api_key="test-internal-key",  # type: ignore[arg-type]
        whatsapp_access_token="graph-token",  # type: ignore[arg-type]
        whatsapp_phone_number_id="PNID",
        whatsapp_app_secret=APP_SECRET,  # type: ignore[arg-type]
        whatsapp_verify_token=VERIFY_TOKEN,  # type: ignore[arg-type]
    )
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        graph = StubGraph()
        container = build_container(settings)
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
                # No model gateway: this test is about the Meta boundary and the
                # wiring behind it, and it stays at zero paid calls.
                gateway_factory=None,
            )
        )
        set_container(app, container)
        yield client, graph
    await dispose_engine()


async def test_the_subscribe_handshake_echoes_the_challenge(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """Meta will not register a webhook that fails this."""
    client, _ = wired
    response = await client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "9876543210",
        },
    )
    assert response.status_code == 200
    assert response.text == "9876543210"


async def test_a_wrong_verify_token_is_refused(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    client, _ = wired
    response = await client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "guessed",
            "hub.challenge": "9876543210",
        },
    )
    assert response.status_code == 403


async def test_a_students_message_gets_an_answer_on_whatsapp(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """The whole path: Meta POST -> answer delivered to the student's number."""
    client, graph = wired
    body = meta_text_payload("Explain photosynthesis", wamid="wamid.e2e.1")

    response = await client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"x-hub-signature-256": sign(body), "content-type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "handled": 1}
    assert graph.sent, "nothing was delivered to the student"
    assert graph.sent[0][0] == STUDENT
    assert graph.texts and graph.texts[0].strip()


async def test_the_turn_is_persisted_under_the_students_subject(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """A WhatsApp turn must be an ordinary conversation, not a special case:
    the admin console, history and memory all read these tables."""
    client, _ = wired
    body = meta_text_payload("What is a derivative?", wamid="wamid.e2e.2")
    await client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"x-hub-signature-256": sign(body)},
    )

    async with get_session_factory()() as db:
        conversations = (await db.execute(select(Conversation))).scalars().all()
        assert len(conversations) == 1
        messages = (
            (
                await db.execute(
                    select(Message).where(Message.conversation_id == conversations[0].id)
                )
            )
            .scalars()
            .all()
        )
        roles = [m.role for m in messages]
        assert "STUDENT" in roles and "ASSISTANT" in roles


async def test_an_unsigned_payload_changes_nothing(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """Without this the endpoint is a public button that spends the model
    budget and writes into a student's history."""
    client, graph = wired
    body = meta_text_payload("free money please", wamid="wamid.forged")

    response = await client.post("/webhooks/whatsapp", content=body)

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert graph.sent == []
    async with get_session_factory()() as db:
        assert (await db.execute(select(Conversation))).scalars().all() == []


async def test_a_forged_signature_changes_nothing(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    client, graph = wired
    body = meta_text_payload("hello", wamid="wamid.forged.2")

    response = await client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"x-hub-signature-256": sign(body, "not-the-app-secret")},
    )

    assert response.json()["status"] == "rejected"
    assert graph.sent == []


async def test_a_redelivery_is_answered_once(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """Meta redelivers whenever it does not see a prompt 2xx. The second
    delivery must replay, not re-answer: re-answering pays twice and sends the
    student the same message twice.
    """
    client, graph = wired
    body = meta_text_payload("Explain inertia", wamid="wamid.retry.1")
    headers = {"x-hub-signature-256": sign(body)}

    first = await client.post("/webhooks/whatsapp", content=body, headers=headers)
    delivered_after_first = len(graph.sent)
    second = await client.post("/webhooks/whatsapp", content=body, headers=headers)

    assert first.json()["handled"] == 1
    assert second.json()["handled"] == 1
    assert len(graph.sent) == delivered_after_first, "the same answer was sent twice"

    async with get_session_factory()() as db:
        assert len((await db.execute(select(Conversation))).scalars().all()) == 1


async def test_a_read_receipt_is_acknowledged_and_ignored(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    client, graph = wired
    body = json.dumps(
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
                                "statuses": [
                                    {
                                        "id": "wamid.out",
                                        "status": "read",
                                        "recipient_id": STUDENT,
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()

    response = await client.post(
        "/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sign(body)}
    )

    assert response.json() == {"status": "ignored", "handled": 0}
    assert graph.sent == []


async def test_two_students_in_one_batch_both_get_answers(
    wired: tuple[AsyncClient, StubGraph],
) -> None:
    """Meta batches. Handling only the first message would silently drop the
    second student's question."""
    client, graph = wired
    payload = json.loads(meta_text_payload("first question", wamid="wamid.batch.1"))
    payload["entry"][0]["changes"][0]["value"]["messages"].append(
        {
            "from": "919999000456",
            "id": "wamid.batch.2",
            "timestamp": "1767225601",
            "type": "text",
            "text": {"body": "second question"},
        }
    )
    body = json.dumps(payload).encode()

    response = await client.post(
        "/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sign(body)}
    )

    assert response.json()["handled"] == 2
    assert {to for to, _ in graph.sent} == {STUDENT, "919999000456"}
