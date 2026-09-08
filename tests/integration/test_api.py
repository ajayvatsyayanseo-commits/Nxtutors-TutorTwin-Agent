"""API surface: validation, correlation IDs, health, size guard, auth."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tutortwin.api.app import create_app
from tutortwin.api.dependencies import build_container, set_container
from tutortwin.config import Settings
from tutortwin.db.engine import dispose_engine, get_session_factory
from tutortwin.orchestration.entry_service import EntryDependencies, TutorTwinEntryService
from tutortwin.providers.fakes import (
    FakeEntitlementGateway,
    FakeIdentityGateway,
    FakeOutboundGateway,
    FakeTutorGateway,
    SystemClock,
)

from ..conftest import TEST_DSN

pytestmark = pytest.mark.integration


def valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_id": "evt_api_1",
        "request_id": "req_api_1",
        "correlation_id": "corr_api_1",
        "source": "test_harness",
        "subject": {"external_type": "test_phone", "external_id": "+919999000001"},
        "message": {
            "message_id": "msg_api_1",
            "type": "TEXT",
            "text": "Explain quadratic equations",
        },
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
    }
    payload.update(overrides)
    return payload


@pytest_asyncio.fixture
async def client(session: object) -> AsyncIterator[AsyncClient]:
    """App wired to the test database. `session` gives us truncated tables."""
    await dispose_engine()
    settings = Settings(
        environment="test",
        database_url=TEST_DSN,  # type: ignore[arg-type]
        internal_api_key="test-internal-key",  # type: ignore[arg-type]
        max_request_bytes=4096,
    )
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
        app.router.lifespan_context(app),
    ):
        # Override the default container so the test subject is Pro-entitled;
        # the FREE path has its own dedicated test.
        container = build_container(settings)
        identity = FakeIdentityGateway()
        outbound = FakeOutboundGateway()
        container.identity = identity
        container.outbound = outbound
        container.entry_service = TutorTwinEntryService(
            EntryDependencies(
                identity=identity,
                entitlement=FakeEntitlementGateway(plans={"+919999000001": "PRO"}),
                tutor=FakeTutorGateway(),
                outbound=outbound,
                clock=SystemClock(),
                session_factory=get_session_factory(),
                # No gateway: the API surface is under test here, not the model
                # path, and this keeps these tests at zero provider calls.
                gateway_factory=None,
            )
        )
        set_container(app, container)
        yield c
    await dispose_engine()


async def test_healthz_does_not_touch_dependencies(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_checks_database(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"


async def test_request_id_propagates_from_caller(client: AsyncClient) -> None:
    """Mandatory proof: an inbound trace id survives the round trip."""
    response = await client.post(
        "/v1/events",
        json=valid_payload(),
        headers={
            "x-internal-key": "test-internal-key",
            "x-request-id": "req_from_caller",
            "x-correlation-id": "corr_from_caller",
        },
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req_from_caller"
    assert response.headers["x-correlation-id"] == "corr_from_caller"


async def test_request_id_generated_when_absent(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers["x-request-id"].startswith("req_")
    assert response.headers["x-correlation-id"].startswith("corr_")


async def test_missing_internal_key_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/v1/events", json=valid_payload())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_wrong_internal_key_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/events", json=valid_payload(), headers={"x-internal-key": "wrong"}
    )
    assert response.status_code == 401


async def test_malformed_payload_returns_structured_error(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/events",
        json={"event_id": "evt_1"},
        headers={"x-internal-key": "test-internal-key"},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert error["fields"]


async def test_validation_error_does_not_echo_student_content(client: AsyncClient) -> None:
    secret_text = "my-private-homework-content"
    response = await client.post(
        "/v1/events",
        json=valid_payload(message={"message_id": "m", "type": "NOPE", "text": secret_text}),
        headers={"x-internal-key": "test-internal-key"},
    )
    assert response.status_code == 422
    assert secret_text not in response.text


async def test_oversized_body_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/events",
        json=valid_payload(message={"message_id": "m", "type": "TEXT", "text": "x" * 9000}),
        headers={"x-internal-key": "test-internal-key"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_full_event_round_trip_and_ownership(client: AsyncClient) -> None:
    headers = {"x-internal-key": "test-internal-key"}
    posted = await client.post("/v1/events", json=valid_payload(), headers=headers)
    assert posted.status_code == 200
    body = posted.json()
    assert body["status"] == "COMPLETED"
    assert body["usage"]["paid_model_calls"] == 0
    conversation_id = body["conversation_id"]

    # Owner reads it.
    owned = await client.get(
        f"/v1/conversations/{conversation_id}",
        params={
            "subject_external_type": "test_phone",
            "subject_external_id": "+919999000001",
        },
        headers=headers,
    )
    assert owned.status_code == 200
    assert len(owned.json()["messages"]) == 2

    # A different student gets 404, not 403 - existence is not disclosed.
    intruder = await client.get(
        f"/v1/conversations/{conversation_id}",
        params={
            "subject_external_type": "test_phone",
            "subject_external_id": "+919999000009",
        },
        headers=headers,
    )
    assert intruder.status_code == 404


async def test_duplicate_post_is_idempotent_over_http(client: AsyncClient) -> None:
    headers = {"x-internal-key": "test-internal-key"}
    first = await client.post("/v1/events", json=valid_payload(), headers=headers)
    second = await client.post("/v1/events", json=valid_payload(), headers=headers)

    assert first.json()["conversation_id"] == second.json()["conversation_id"]
    assert first.json()["idempotent_replay"] is False
    assert second.json()["idempotent_replay"] is True


# --- internal job endpoint ----------------------------------------------------


async def test_internal_job_endpoint_requires_authentication(client: AsyncClient) -> None:
    """Unauthenticated callers must never be able to trigger media processing."""
    import uuid

    response = await client.post("/internal/jobs/run", json={"job_id": str(uuid.uuid4())})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_internal_job_endpoint_rejects_a_wrong_key(client: AsyncClient) -> None:
    import uuid

    response = await client.post(
        "/internal/jobs/run",
        json={"job_id": str(uuid.uuid4())},
        headers={"x-internal-key": "wrong"},
    )
    assert response.status_code == 401


async def test_unknown_job_is_reported_not_found(client: AsyncClient) -> None:
    import uuid

    response = await client.post(
        "/internal/jobs/run",
        json={"job_id": str(uuid.uuid4())},
        headers={"x-internal-key": "test-internal-key"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "NOT_FOUND"


async def test_job_payload_rejects_extra_fields(client: AsyncClient) -> None:
    """Cloud Tasks sends only a job id; anything else is a malformed caller."""
    import uuid

    response = await client.post(
        "/internal/jobs/run",
        json={"job_id": str(uuid.uuid4()), "media_bytes": "injected"},
        headers={"x-internal-key": "test-internal-key"},
    )
    assert response.status_code == 422
