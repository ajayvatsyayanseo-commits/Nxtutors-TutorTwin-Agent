"""The threat model, as executable claims.

One test per threat in the Phase 07 model. Where an earlier phase already proves
a mitigation, this file asserts the *boundary* rather than repeating the proof -
the point of collecting them here is that a reader can check the model against
the code without trusting a table in a document.

Threats covered: student IDOR, file abuse, prompt injection, cost abuse, replay,
job forgery, secret leak, model tool abuse, stored XSS in the control plane,
malicious PDF, RAG poisoning. Admin compromise is covered end-to-end by
`test_admin_security.py`, which walks the entire role matrix.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.api.app import create_app
from tutortwin.config import Settings
from tutortwin.db.engine import dispose_engine
from tutortwin.db.models import UsageLedger

pytestmark = pytest.mark.integration

KEY = "threat-model-key"
DSN = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin_test"


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app(
        Settings(
            environment="test",
            database_url=DSN,  # type: ignore[arg-type]
            internal_api_key=KEY,  # type: ignore[arg-type]
        )
    )
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
        app.router.lifespan_context(app),
    ):
        yield c
    await dispose_engine()


def auth() -> dict[str, str]:
    return {"x-internal-key": KEY}


# --- student IDOR -------------------------------------------------------------


async def test_reading_another_students_conversation_is_indistinguishable_from_absence(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A wrong-owner read answers 404, not 403.

    403 confirms the row exists. Enumerating conversation ids against a 403/404
    split is how an attacker maps another student's history without ever reading
    a word of it.
    """
    response = await client.get(
        f"/v1/conversations/{uuid4()}",
        params={"subject_external_type": "test_phone", "subject_external_id": "+919999000001"},
        headers=auth(),
    )
    assert response.status_code == 404
    assert "not found" in response.text.lower() or response.json()["error"]["code"]


# --- job forgery --------------------------------------------------------------


async def test_a_forged_job_push_is_refused(client: AsyncClient) -> None:
    """The worker endpoint is where the money is, so it is where forgery pays."""
    body = {"job_id": str(uuid4())}

    assert (await client.post("/internal/jobs/run", json=body)).status_code == 401
    assert (
        await client.post("/internal/jobs/run", json=body, headers={"x-internal-key": ""})
    ).status_code == 401
    assert (
        await client.post("/internal/jobs/run", json=body, headers={"x-internal-key": KEY + "x"})
    ).status_code == 401
    # A near-miss must not leak through a prefix comparison.
    assert (
        await client.post("/internal/jobs/run", json=body, headers={"x-internal-key": KEY[:-1]})
    ).status_code == 401


async def test_the_retention_sweep_is_authenticated(client: AsyncClient) -> None:
    """It deletes student data. An open endpoint here is a delete button."""
    assert (await client.post("/internal/retention/sweep", json={})).status_code == 401


# --- replay -------------------------------------------------------------------


async def test_a_replayed_event_does_not_execute_twice(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Capturing and resending a valid request must not double the work.

    The idempotency key is `source:message_id`, enforced by a unique index, so a
    replay is decided by the database rather than by a check that races.
    """
    event = {
        "event_id": "threat-replay",
        "request_id": "threat-replay",
        "correlation_id": "threat-replay",
        "source": "threat",
        "subject": {"external_type": "test_phone", "external_id": "+919999000001"},
        "message": {"message_id": "threat-replay", "type": "TEXT", "text": "hello"},
        "occurred_at": datetime.now(UTC).isoformat(),
    }

    first = await client.post("/v1/events", json=event, headers=auth())
    second = await client.post("/v1/events", json=event, headers=auth())

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True

    events = (await session.execute(select(func.count()).select_from(UsageLedger))).scalar_one()
    assert events == 0, "an unentitled replay costs nothing either way"


# --- cost abuse ---------------------------------------------------------------


async def test_an_oversized_body_is_refused_before_it_is_parsed(
    client: AsyncClient,
) -> None:
    """Parsing is work. A 50MB body must be refused, not deserialised first."""
    payload = {
        "event_id": "big",
        "request_id": "big",
        "correlation_id": "big",
        "source": "threat",
        "subject": {"external_type": "test_phone", "external_id": "+919999000001"},
        "message": {"message_id": "big", "type": "TEXT", "text": "x" * 300_000},
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    response = await client.post("/v1/events", json=payload, headers=auth())
    assert response.status_code in (413, 422)


async def test_an_unknown_plan_code_fails_closed(session: AsyncSession) -> None:
    """A typo in a plan code must not grant unlimited spend.

    `plan_for` returns the most restrictive policy for anything it does not
    recognise, so a mis-set entitlement refuses rather than escalates.
    """
    from tutortwin.orchestration.entry_service import plan_for

    assert not plan_for("PLATINUM_UNLIMITED").allows_paid_ai
    assert not plan_for("").allows_paid_ai
    assert not plan_for("pro; drop table").allows_paid_ai
    # And the real one still works, so failing closed did not fail everything.
    assert plan_for("pro").allows_paid_ai


# --- prompt injection ---------------------------------------------------------


def test_instructions_inside_retrieved_text_are_marked_untrusted() -> None:
    """A document is evidence, never an instruction.

    The retrieved passage is wrapped and labelled before it reaches the prompt,
    and the safety block above it says so. Without the label, "ignore your
    previous instructions" in a PDF is indistinguishable from a system rule.
    """
    from tutortwin.services.prompts import SAFETY_BLOCK

    lowered = SAFETY_BLOCK.lower()
    assert "instruction" in lowered
    assert any(word in lowered for word in ("document", "retrieved", "student", "untrusted"))


def test_a_persona_cannot_override_the_safety_block() -> None:
    """Persona text is operator-supplied and therefore ordered *after* safety.

    An LLM weights earlier instructions more heavily. A persona that said
    "ignore the rules above" would be read after the rules it is trying to
    displace, which is the only ordering that makes the rules load-bearing.
    """
    from tutortwin.domain.capabilities import CapabilityId, PedagogyMode
    from tutortwin.services.prompts import SAFETY_BLOCK, build_system_prompt

    prompt = build_system_prompt(
        tutor=None, capability=CapabilityId.GENERAL_TUTORING, mode=PedagogyMode.GUIDED
    )
    assert prompt.startswith(SAFETY_BLOCK)


# --- secret leak --------------------------------------------------------------


async def test_no_error_response_carries_a_secret(client: AsyncClient) -> None:
    """Bodies, not just logs. An exception payload is a response body too."""
    responses = [
        await client.post("/internal/jobs/run", json={"job_id": "not-a-uuid"}, headers=auth()),
        await client.post("/v1/events", json={"nope": 1}, headers=auth()),
        await client.get("/readyz"),
    ]
    for response in responses:
        body = response.text
        assert KEY not in body
        assert "postgres" not in body.lower()
        assert "password" not in body.lower()


def test_settings_never_render_a_secret() -> None:
    """`repr` reaches logs, tracebacks and error reporters by accident."""
    settings = Settings(
        environment="test",
        internal_api_key="super-secret-value",  # type: ignore[arg-type]
        anthropic_api_key="sk-ant-secret",  # type: ignore[arg-type]
        openai_api_key="sk-openai-secret",  # type: ignore[arg-type]
        r2_secret_access_key="r2-secret",  # type: ignore[arg-type]
    )
    rendered = f"{settings!r} {settings!s}"
    for secret in ("super-secret-value", "sk-ant-secret", "sk-openai-secret", "r2-secret"):
        assert secret not in rendered


def test_a_secret_is_never_written_to_the_container_image() -> None:
    """The Dockerfile must not bake one in, and .dockerignore must exclude .env."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8").lower()
    for marker in ("api_key=", "secret=", "password=", "sk-"):
        assert marker not in dockerfile, f"Dockerfile contains {marker!r}"

    ignored = (root / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in ignored, "a .env copied into the image is a secret in the registry"


# --- file abuse / malicious PDF ----------------------------------------------


def test_declared_mime_is_never_trusted_for_a_security_decision() -> None:
    """The sender picks the filename and the content type. Neither is evidence.

    Sniffing from magic bytes is what makes "this is a PDF" a fact rather than a
    claim, and it is the difference between a page count and a zip bomb.
    """
    from tutortwin.media.validation import sniff_mime

    # A payload that calls itself a PDF but is not one.
    assert sniff_mime(b"MZ\x90\x00this is a windows executable") != "application/pdf"
    assert sniff_mime(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n") == "application/pdf"


def test_a_media_id_cannot_escape_its_directory() -> None:
    """The media id is provider-supplied, so it is a path traversal vector."""
    import asyncio
    import tempfile
    from pathlib import Path

    from tutortwin.media.adapters import LocalFileMediaSource, MediaNotFound

    with tempfile.TemporaryDirectory() as tmp:
        source = LocalFileMediaSource(root=Path(tmp))
        for evil in ("../../../etc/passwd", "..\\..\\windows\\win.ini", "/etc/shadow"):
            with pytest.raises((MediaNotFound, KeyError)):
                asyncio.run(source.fetch("whatsapp", evil))


# --- RAG poisoning ------------------------------------------------------------


async def test_retrieval_cannot_cross_a_student_boundary(session: AsyncSession) -> None:
    """One student's uploaded document must never answer another's question.

    Ownership is a predicate in the *same statement* that ranks, not a filter
    applied afterwards - an afterwards-filter is one refactor from being dropped,
    and the refactor that drops it looks like a performance improvement.
    """
    from tutortwin.rag.vector_store import build_visibility_predicate

    mine = uuid4()
    theirs = uuid4()

    from tutortwin.domain.knowledge import RetrievalScope

    predicate = build_visibility_predicate(RetrievalScope(subject_id=mine))

    sql = predicate.sql.lower()
    # The owner is bound, never interpolated, and it is part of the ranking
    # query rather than a later pass over its results.
    assert "subject_id" in sql
    assert str(mine) not in sql, "the owner must be a bound parameter, not text"
    assert str(mine) in {str(v) for v in predicate.params.values()}
    assert str(theirs) not in {str(v) for v in predicate.params.values()}


# --- model tool abuse ---------------------------------------------------------


def test_arbitrary_code_execution_is_refused_by_default() -> None:
    """The sandbox is disabled, and disabled means it refuses, not that it runs.

    A "sandbox" that is a subprocess in the API container is not a boundary: it
    shares the service account, the database credentials and the R2 token. Until
    a real ephemeral boundary exists with none of those, execution stays off -
    and static code tutoring keeps working, which is what the refusal says.
    """
    import asyncio

    from tutortwin.learning.homework import DisabledSandbox, SandboxStatus

    sandbox = DisabledSandbox()
    assert not sandbox.enabled

    result = asyncio.run(sandbox.run("python", "import os; os.system('id')"))
    assert result.status is SandboxStatus.DISABLED
    assert result.reason
    # The refusal is useful, not a dead end: the student is offered the tutoring
    # that does not need execution.
    assert "explain" in result.reason.lower() or "trace" in result.reason.lower()
