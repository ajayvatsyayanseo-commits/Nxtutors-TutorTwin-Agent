"""Admin control-plane security, proved against the real API and database.

Each test states a property an attacker would try to break: get in without a
session, act above your role, read a secret, replay a cookie cross-site, or make
the filter parameters do something they should not. A control plane that is wrong
about any one of these is worse than no control plane, because it looks like one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.api.admin_deps import CSRF_HEADER, SESSION_COOKIE, SESSION_HEADER
from tutortwin.api.app import create_app
from tutortwin.config import Settings
from tutortwin.db.admin_models import AdminSession, AdminUser
from tutortwin.db.engine import dispose_engine
from tutortwin.db.models import (
    AuditEvent,
    FeatureFlag,
    RequestEvent,
    RequestState,
    Subject,
    UsageLedger,
)
from tutortwin.domain.admin import AdminRole, Permission, permissions_for
from tutortwin.repositories import admin as admin_repo
from tutortwin.security import admin_auth

from ..conftest import TEST_DSN

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await dispose_engine()
    settings = Settings(
        environment="test",
        database_url=TEST_DSN,  # type: ignore[arg-type]
        internal_api_key="test-internal-key",  # type: ignore[arg-type]
    )
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
        app.router.lifespan_context(app),
    ):
        yield c


async def make_admin(
    session: AsyncSession,
    *,
    email: str,
    role: AdminRole,
    password: str = PASSWORD,
    must_change_password: bool = False,
) -> AdminUser:
    user = await admin_repo.create_admin(
        session,
        email=email,
        password=password,
        role=role,
        display_name=email.split("@")[0],
        must_change_password=must_change_password,
    )
    await session.commit()
    return user


async def sign_in(client: AsyncClient, email: str, password: str = PASSWORD) -> tuple[str, str]:
    """Returns (session token, csrf token)."""
    response = await client.post(
        "/v1/admin/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get(SESSION_COOKIE)
    assert token, "login must set the session cookie"
    return token, response.json()["csrf_token"]


def auth(token: str, csrf: str | None = None) -> dict[str, str]:
    headers = {SESSION_HEADER: token}
    if csrf:
        headers[CSRF_HEADER] = csrf
    return headers


# --- 1. unauthenticated rejection ---------------------------------------------

PROTECTED_GETS = (
    "/v1/admin/auth/me",
    "/v1/admin/dashboard",
    "/v1/admin/students",
    "/v1/admin/conversations",
    "/v1/admin/documents",
    "/v1/admin/tutors",
    "/v1/admin/plans",
    "/v1/admin/models",
    "/v1/admin/prompts",
    "/v1/admin/flags",
    "/v1/admin/jobs",
    "/v1/admin/costs",
    "/v1/admin/audit",
    "/v1/admin/admins",
    "/v1/admin/learning/assessments",
)


@pytest.mark.parametrize("path", PROTECTED_GETS)
async def test_every_admin_read_requires_a_session(client: AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 401, f"{path} answered {response.status_code}"
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_a_forged_session_token_is_rejected(client: AsyncClient) -> None:
    """The stored value is a hash, so a guessed token cannot be reversed into one."""
    response = await client.get("/v1/admin/dashboard", headers=auth("not-a-real-token"))
    assert response.status_code == 401


async def test_internal_service_key_grants_no_admin_access(client: AsyncClient) -> None:
    """The shared secret authenticates Cloud Tasks, not an administrator.

    These are separate authorities on purpose: a leaked internal key must not
    become a login.
    """
    response = await client.get(
        "/v1/admin/students", headers={"x-internal-key": "test-internal-key"}
    )
    assert response.status_code == 401


# --- 2. RBAC ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "path", "allowed"),
    [
        (AdminRole.USAGE_VIEWER, "/v1/admin/costs", True),
        (AdminRole.USAGE_VIEWER, "/v1/admin/students", False),
        (AdminRole.USAGE_VIEWER, "/v1/admin/audit", False),
        (AdminRole.SUPPORT, "/v1/admin/students", True),
        (AdminRole.SUPPORT, "/v1/admin/costs", False),
        (AdminRole.SUPPORT, "/v1/admin/admins", False),
        (AdminRole.TUTOR_VIEWER, "/v1/admin/tutors", True),
        (AdminRole.TUTOR_VIEWER, "/v1/admin/dashboard", False),
        (AdminRole.ACADEMIC_ADMIN, "/v1/admin/prompts", True),
        (AdminRole.ACADEMIC_ADMIN, "/v1/admin/costs", False),
        (AdminRole.ADMIN, "/v1/admin/costs", True),
        (AdminRole.ADMIN, "/v1/admin/admins", True),
        (AdminRole.SUPER_ADMIN, "/v1/admin/admins", True),
    ],
)
async def test_role_matrix_is_enforced_by_the_server(
    client: AsyncClient, session: AsyncSession, role: AdminRole, path: str, allowed: bool
) -> None:
    """Hiding a button is a courtesy. This is the control."""
    await make_admin(session, email=f"{role.value.lower()}@example.com", role=role)
    token, _ = await sign_in(client, f"{role.value.lower()}@example.com")

    response = await client.get(path, headers=auth(token))
    if allowed:
        assert response.status_code == 200, f"{role} should read {path}: {response.text}"
    else:
        assert response.status_code == 403, f"{role} must not read {path}"
        assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_support_cannot_flip_a_kill_switch(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_admin(session, email="support@example.com", role=AdminRole.SUPPORT)
    token, csrf = await sign_in(client, "support@example.com")

    response = await client.post(
        "/v1/admin/flags/verifier",
        headers=auth(token, csrf),
        json={"enabled": False, "reason": "trying it on", "confirm": True},
    )
    assert response.status_code == 403

    # And nothing was written for that switch.
    count = (
        await session.execute(
            select(func.count()).select_from(FeatureFlag).where(FeatureFlag.key == "verifier")
        )
    ).scalar_one()
    assert count == 0


async def test_permission_matrix_endpoint_matches_the_enforced_matrix(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The UI is told the same rules the server applies, from the same source."""
    await make_admin(session, email="matrix@example.com", role=AdminRole.ADMIN)
    token, _ = await sign_in(client, "matrix@example.com")

    response = await client.get("/v1/admin/roles", headers=auth(token))
    assert response.status_code == 200
    served = response.json()
    for role in AdminRole:
        assert served[str(role)] == sorted(str(p) for p in permissions_for(role))


# --- 3. privilege escalation --------------------------------------------------


async def test_admin_cannot_create_administrators(
    client: AsyncClient, session: AsyncSession
) -> None:
    """ADMIN runs the platform; only SUPER_ADMIN can grant authority.

    Without this split, every operator is one API call away from being the most
    powerful account in the system.
    """
    await make_admin(session, email="ops@example.com", role=AdminRole.ADMIN)
    token, csrf = await sign_in(client, "ops@example.com")

    response = await client.post(
        "/v1/admin/admins",
        headers=auth(token, csrf),
        json={
            "email": "newguy@example.com",
            "role": "SUPER_ADMIN",
            "password": "another-long-password",
            "reason": "escalation attempt",
            "confirm": True,
        },
    )
    assert response.status_code == 403
    assert (
        await session.execute(
            select(func.count())
            .select_from(AdminUser)
            .where(AdminUser.email == "newguy@example.com")
        )
    ).scalar_one() == 0


async def test_support_cannot_promote_itself(client: AsyncClient, session: AsyncSession) -> None:
    support = await make_admin(session, email="s@example.com", role=AdminRole.SUPPORT)
    token, csrf = await sign_in(client, "s@example.com")

    response = await client.post(
        f"/v1/admin/admins/{support.id}/role",
        headers=auth(token, csrf),
        json={"role": "SUPER_ADMIN", "reason": "promoting myself", "confirm": True},
    )
    assert response.status_code == 403

    await session.refresh(support)
    assert support.role == str(AdminRole.SUPPORT)


async def test_super_admin_cannot_demote_itself(client: AsyncClient, session: AsyncSession) -> None:
    """Locking the whole team out is a failure mode, not a permission."""
    root = await make_admin(session, email="root@example.com", role=AdminRole.SUPER_ADMIN)
    token, csrf = await sign_in(client, "root@example.com")

    response = await client.post(
        f"/v1/admin/admins/{root.id}/role",
        headers=auth(token, csrf),
        json={"role": "SUPPORT", "reason": "reducing my own access", "confirm": True},
    )
    assert response.status_code == 403
    await session.refresh(root)
    assert root.role == str(AdminRole.SUPER_ADMIN)


async def test_role_change_revokes_the_targets_sessions(
    client: AsyncClient, session: AsyncSession
) -> None:
    root = await make_admin(session, email="root2@example.com", role=AdminRole.SUPER_ADMIN)
    victim = await make_admin(session, email="victim@example.com", role=AdminRole.ADMIN)

    victim_token, _ = await sign_in(client, "victim@example.com")
    assert (await client.get("/v1/admin/dashboard", headers=auth(victim_token))).status_code == 200

    root_token, root_csrf = await sign_in(client, "root2@example.com")
    demote = await client.post(
        f"/v1/admin/admins/{victim.id}/role",
        headers=auth(root_token, root_csrf),
        json={"role": "USAGE_VIEWER", "reason": "role corrected after review", "confirm": True},
    )
    assert demote.status_code == 200, demote.text
    assert root.id != victim.id

    # The old session is dead, not merely less powerful.
    after = await client.get("/v1/admin/dashboard", headers=auth(victim_token))
    assert after.status_code == 401


async def test_disabled_account_loses_access_immediately(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_admin(session, email="root3@example.com", role=AdminRole.SUPER_ADMIN)
    target = await make_admin(session, email="leaver@example.com", role=AdminRole.ADMIN)
    leaver_token, _ = await sign_in(client, "leaver@example.com")

    root_token, root_csrf = await sign_in(client, "root3@example.com")
    response = await client.post(
        f"/v1/admin/admins/{target.id}/status",
        headers=auth(root_token, root_csrf),
        json={"status": "DISABLED", "reason": "left the company", "confirm": True},
    )
    assert response.status_code == 204

    assert (await client.get("/v1/admin/dashboard", headers=auth(leaver_token))).status_code == 401
    assert (
        await client.post(
            "/v1/admin/auth/login",
            json={"email": "leaver@example.com", "password": PASSWORD},
        )
    ).status_code == 401


# --- 4. secrets are absent ----------------------------------------------------


async def test_no_response_carries_a_password_hash_or_token(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_admin(session, email="root4@example.com", role=AdminRole.SUPER_ADMIN)
    token, _ = await sign_in(client, "root4@example.com")

    for path in ("/v1/admin/admins", "/v1/admin/auth/me", "/v1/admin/models"):
        body = (await client.get(path, headers=auth(token))).text
        for forbidden in ("password_hash", "$argon2", "token_sha256", "csrf_sha256", "totp_secret"):
            assert forbidden not in body, f"{path} leaked {forbidden}"


async def test_model_catalog_reports_key_presence_not_key_value(
    client: AsyncClient, session: AsyncSession
) -> None:
    """An operator needs to know whether a provider is wired, never the secret."""
    await make_admin(session, email="root5@example.com", role=AdminRole.SUPER_ADMIN)
    token, _ = await sign_in(client, "root5@example.com")

    payload = (await client.get("/v1/admin/models", headers=auth(token))).json()
    assert set(payload["provider_key_configured"]) == {"openai", "anthropic"}
    assert all(isinstance(v, bool) for v in payload["provider_key_configured"].values())
    assert "api_key" not in (await client.get("/v1/admin/models", headers=auth(token))).text


async def test_session_token_is_not_stored_in_the_clear(session: AsyncSession) -> None:
    """A database dump must not yield a working session."""
    user = await make_admin(session, email="hashcheck@example.com", role=AdminRole.ADMIN)
    issued = await admin_auth.login(session, email=user.email, password=PASSWORD)

    rows = list((await session.execute(select(AdminSession))).scalars())
    assert rows, "a session row must exist"
    stored = {row.token_sha256 for row in rows}
    assert issued.session_token not in stored
    assert issued.csrf_token not in {row.csrf_sha256 for row in rows}
    assert all(len(value) == 64 for value in stored)


# --- 5. CSRF and session lifetime --------------------------------------------


async def test_mutation_without_the_csrf_header_is_refused(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The cookie proves who. It does not prove the request came from our page."""
    await make_admin(session, email="csrf@example.com", role=AdminRole.SUPER_ADMIN)
    token, csrf = await sign_in(client, "csrf@example.com")

    without = await client.post(
        "/v1/admin/flags/verifier",
        headers=auth(token),
        json={"enabled": False, "reason": "cross-site attempt", "confirm": True},
    )
    assert without.status_code == 403
    assert "CSRF" in without.json()["error"]["message"]

    with_token = await client.post(
        "/v1/admin/flags/verifier",
        headers=auth(token, csrf),
        json={"enabled": False, "reason": "legitimate incident response", "confirm": True},
    )
    assert with_token.status_code == 200


async def test_a_csrf_token_from_another_session_does_not_work(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_admin(session, email="a@example.com", role=AdminRole.SUPER_ADMIN)
    await make_admin(session, email="b@example.com", role=AdminRole.SUPER_ADMIN)
    token_a, _ = await sign_in(client, "a@example.com")
    _, csrf_b = await sign_in(client, "b@example.com")

    response = await client.post(
        "/v1/admin/flags/verifier",
        headers=auth(token_a, csrf_b),
        json={"enabled": False, "reason": "mixed session tokens", "confirm": True},
    )
    assert response.status_code == 403


async def test_logout_revokes_server_side(client: AsyncClient, session: AsyncSession) -> None:
    await make_admin(session, email="bye@example.com", role=AdminRole.ADMIN)
    token, csrf = await sign_in(client, "bye@example.com")

    assert (
        await client.post("/v1/admin/auth/logout", headers=auth(token, csrf))
    ).status_code == 204
    assert (await client.get("/v1/admin/auth/me", headers=auth(token))).status_code == 401


async def test_expired_session_is_rejected(client: AsyncClient, session: AsyncSession) -> None:
    await make_admin(session, email="stale@example.com", role=AdminRole.ADMIN)
    token, _ = await sign_in(client, "stale@example.com")

    row = (await session.execute(select(AdminSession))).scalars().first()
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    assert (await client.get("/v1/admin/auth/me", headers=auth(token))).status_code == 401


async def test_idle_session_expires_without_a_sweeper(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Enforced on read, so it holds even if no background job ever runs."""
    await make_admin(session, email="idle@example.com", role=AdminRole.ADMIN)
    token, _ = await sign_in(client, "idle@example.com")

    row = (await session.execute(select(AdminSession))).scalars().first()
    assert row is not None
    row.last_seen_at = datetime.now(UTC) - timedelta(
        minutes=admin_auth.SESSION_IDLE_TIMEOUT_MINUTES + 1
    )
    await session.commit()

    assert (await client.get("/v1/admin/auth/me", headers=auth(token))).status_code == 401


# --- 6. login hardening -------------------------------------------------------


async def test_unknown_and_wrong_password_are_indistinguishable(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_admin(session, email="known@example.com", role=AdminRole.ADMIN)

    unknown = await client.post(
        "/v1/admin/auth/login",
        json={"email": "nobody@example.com", "password": "whatever-long-enough"},
    )
    wrong = await client.post(
        "/v1/admin/auth/login",
        json={"email": "known@example.com", "password": "wrong-but-long-enough"},
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json(), "the response must not enumerate accounts"


async def test_account_locks_after_repeated_failures(
    client: AsyncClient, session: AsyncSession
) -> None:
    user = await make_admin(session, email="brute@example.com", role=AdminRole.ADMIN)

    for _ in range(admin_auth.MAX_FAILED_ATTEMPTS):
        await client.post(
            "/v1/admin/auth/login",
            json={"email": "brute@example.com", "password": "wrong-password-here"},
        )

    await session.refresh(user)
    assert user.locked_until is not None

    # Even the correct password is refused while the lock stands.
    correct = await client.post(
        "/v1/admin/auth/login", json={"email": "brute@example.com", "password": PASSWORD}
    )
    assert correct.status_code == 401


async def test_bootstrap_password_blocks_nothing_until_changed(
    client: AsyncClient, session: AsyncSession
) -> None:
    """`must_change_password` is reported so the UI can force the change."""
    await make_admin(
        session, email="fresh@example.com", role=AdminRole.ADMIN, must_change_password=True
    )
    response = await client.post(
        "/v1/admin/auth/login", json={"email": "fresh@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["actor"]["must_change_password"] is True


# --- 7. high-risk actions are audited ----------------------------------------


async def test_high_risk_action_requires_a_reason(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_admin(session, email="reason@example.com", role=AdminRole.SUPER_ADMIN)
    token, csrf = await sign_in(client, "reason@example.com")

    response = await client.post(
        "/v1/admin/flags/verifier",
        headers=auth(token, csrf),
        json={"enabled": False, "reason": "no", "confirm": True},
    )
    assert response.status_code == 422


async def test_high_risk_action_requires_confirmation(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_admin(session, email="confirm@example.com", role=AdminRole.SUPER_ADMIN)
    token, csrf = await sign_in(client, "confirm@example.com")

    response = await client.post(
        "/v1/admin/flags/verifier",
        headers=auth(token, csrf),
        json={"enabled": False, "reason": "forgot to tick the box", "confirm": False},
    )
    assert response.status_code == 422


async def test_kill_switch_writes_an_audit_event_with_before_and_after(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_admin(session, email="audit@example.com", role=AdminRole.SUPER_ADMIN)
    token, csrf = await sign_in(client, "audit@example.com")

    response = await client.post(
        "/v1/admin/flags/rag_retrieval",
        headers=auth(token, csrf),
        json={"enabled": False, "reason": "retrieval latency incident 2026-03-04", "confirm": True},
    )
    assert response.status_code == 200

    event = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "FEATURE_KILL_SWITCH"))
    ).scalar_one()
    assert event.actor_type == "ADMIN"
    assert event.target_id == "rag_retrieval"
    assert event.detail_json["reason"].startswith("retrieval latency")
    assert event.detail_json["high_risk"] is True
    assert event.detail_json["before"] == {"enabled": True}
    assert event.detail_json["after"] == {"enabled": False}


async def test_entitlement_override_is_audited_and_supersedes(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_admin(session, email="ent@example.com", role=AdminRole.SUPER_ADMIN)
    subject = Subject(external_identity_type="test_phone", external_identity_value="+919990001111")
    session.add(subject)
    await session.commit()

    token, csrf = await sign_in(client, "ent@example.com")
    response = await client.post(
        f"/v1/admin/students/{subject.id}/entitlement",
        headers=auth(token, csrf),
        json={
            "plan_code": "PRO",
            "status": "ACTIVE",
            "reason": "goodwill after a billing incident",
            "confirm": True,
        },
    )
    assert response.status_code == 204

    event = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "ENTITLEMENT_OVERRIDE"))
    ).scalar_one()
    assert event.target_id == str(subject.id)
    assert event.detail_json["after"]["plan_code"] == "PRO"


async def test_unknown_kill_switch_key_is_refused(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Inventing a key would create a control that nothing reads."""
    await make_admin(session, email="fake@example.com", role=AdminRole.SUPER_ADMIN)
    token, csrf = await sign_in(client, "fake@example.com")

    response = await client.post(
        "/v1/admin/flags/definitely_not_a_real_switch",
        headers=auth(token, csrf),
        json={"enabled": False, "reason": "made up switch name", "confirm": True},
    )
    assert response.status_code == 422


# --- 8. hostile input ---------------------------------------------------------


@pytest.mark.parametrize(
    "term",
    [
        "'; DROP TABLE tutortwin_subjects; --",
        "' OR '1'='1",
        "%",
        "_",
        "%%%%%%%%",
        "\\",
        "\x00truncated",
        "<script>alert(1)</script>",
    ],
)
async def test_hostile_search_terms_are_safe_and_do_not_widen_the_result(
    client: AsyncClient, session: AsyncSession, term: str
) -> None:
    """`%` and `_` are LIKE wildcards. Unescaped, a search for `_` returns everyone."""
    await make_admin(session, email="search@example.com", role=AdminRole.SUPER_ADMIN)
    for suffix in ("2001", "2002", "2003"):
        session.add(
            Subject(
                external_identity_type="test_phone",
                external_identity_value=f"+91999000{suffix}",
            )
        )
    await session.commit()

    token, _ = await sign_in(client, "search@example.com")
    response = await client.get("/v1/admin/students", headers=auth(token), params={"q": term})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0, f"{term!r} must not match unrelated students"

    # The table is still there.
    assert (await session.execute(select(func.count()).select_from(Subject))).scalar_one() == 3


async def test_page_size_cannot_be_used_to_dump_the_table(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_admin(session, email="page@example.com", role=AdminRole.SUPER_ADMIN)
    token, _ = await sign_in(client, "page@example.com")

    response = await client.get(
        "/v1/admin/students", headers=auth(token), params={"page_size": 100000}
    )
    assert response.status_code == 422


async def test_unknown_query_parameters_do_not_reach_sql(
    client: AsyncClient, session: AsyncSession
) -> None:
    """FastAPI ignores unknown query params; this pins that they cannot filter."""
    await make_admin(session, email="unknown@example.com", role=AdminRole.SUPER_ADMIN)
    session.add(
        Subject(external_identity_type="test_phone", external_identity_value="+919990003333")
    )
    await session.commit()

    token, _ = await sign_in(client, "unknown@example.com")
    response = await client.get(
        "/v1/admin/students",
        headers=auth(token),
        params={"order_by": "1; DROP TABLE tutortwin_subjects", "limit": "999999"},
    )
    assert response.status_code == 200
    assert response.json()["page_size"] == admin_repo.DEFAULT_PAGE_SIZE


async def test_cost_grouping_is_a_closed_set(client: AsyncClient, session: AsyncSession) -> None:
    await make_admin(session, email="cost@example.com", role=AdminRole.SUPER_ADMIN)
    token, _ = await sign_in(client, "cost@example.com")

    ok = await client.get("/v1/admin/costs", headers=auth(token), params={"group_by": "provider"})
    assert ok.status_code == 200

    bad = await client.get(
        "/v1/admin/costs",
        headers=auth(token),
        params={"group_by": "model_alias; DROP TABLE usage_ledger"},
    )
    assert bad.status_code == 422


async def test_every_offered_cost_grouping_answers(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The seven groupings the prompt asks for, plus day, all execute.

    `tutor` and `capability` are the two that are not a plain column - one joins
    the active assignment, the other reads a column that is null for every row
    written before it existed. A grouping that 500s on an empty ledger would only
    be discovered by an operator on the day they needed the number.
    """
    await make_admin(session, email="grouping@example.com", role=AdminRole.SUPER_ADMIN)
    token, _ = await sign_in(client, "grouping@example.com")

    subject = Subject(external_identity_type="test_phone", external_identity_value="+919990007777")
    session.add(subject)
    await session.flush()
    session.add(
        UsageLedger(
            subject_id=subject.id,
            provider="fake",
            model_alias="VISION",
            capability="IMAGE_QA",
            input_tokens=100,
            output_tokens=20,
            cached_tokens=40,
            estimated_cost_micros=1234,
        )
    )
    await session.commit()

    for group_by in (
        "model",
        "provider",
        "capability",
        "student",
        "tutor",
        "media",
        "verification",
        "day",
    ):
        response = await client.get(
            "/v1/admin/costs", headers=auth(token), params={"group_by": group_by}
        )
        assert response.status_code == 200, f"{group_by}: {response.text}"
        body = response.json()
        assert body["group_by"] == group_by
        assert body["total_cost_micros"] == 1234, group_by

    # The one row is a vision call by an unassigned student, and each grouping
    # says so in its own terms rather than dropping it.
    for group_by, expected in (
        ("media", "VISION"),
        ("verification", "answering"),
        ("capability", "IMAGE_QA"),
        ("tutor", "unassigned"),
    ):
        response = await client.get(
            "/v1/admin/costs", headers=auth(token), params={"group_by": group_by}
        )
        assert [b["key"] for b in response.json()["buckets"]] == [expected], group_by


async def test_dashboard_reports_latency_and_cache_share(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Latency is null when nothing completed, not zero.

    Zero would read as "instant" on a dashboard whose whole job is to say what
    students are experiencing, and an operator cannot tell a fast service from
    an idle one by looking at a 0.
    """
    await make_admin(session, email="latency@example.com", role=AdminRole.SUPER_ADMIN)
    token, _ = await sign_in(client, "latency@example.com")

    response = await client.get("/v1/admin/dashboard", headers=auth(token))
    assert response.status_code == 200
    counts = response.json()["counts"]
    assert counts["latency_p50_ms"] is None
    assert counts["latency_p95_ms"] is None
    assert counts["input_tokens"] == 0
    assert counts["cached_input_tokens"] == 0

    started = datetime.now(UTC) - timedelta(seconds=2)
    event = RequestEvent(
        event_id="lat-1",
        request_id="lat-1",
        correlation_id="lat-1",
        source="test",
        message_type="TEXT",
        contract_version="1.0",
        occurred_at=started,
        created_at=started,
    )
    session.add(event)
    await session.flush()
    session.add(
        RequestState(
            request_event_id=event.id,
            status="COMPLETED",
            created_at=started + timedelta(milliseconds=1500),
        )
    )
    session.add(
        UsageLedger(
            provider="fake",
            model_alias="CHEAP_TEXT",
            input_tokens=1000,
            cached_tokens=250,
            estimated_cost_micros=10,
        )
    )
    await session.commit()

    counts = (await client.get("/v1/admin/dashboard", headers=auth(token))).json()["counts"]
    assert counts["latency_p50_ms"] == 1500
    assert counts["latency_p95_ms"] == 1500
    assert counts["cached_input_tokens"] == 250
    assert counts["input_tokens"] == 1000


# --- 9. student data boundaries ----------------------------------------------


async def test_admin_reads_are_scoped_by_the_requested_student(
    client: AsyncClient, session: AsyncSession
) -> None:
    """An operator can see any student, but only the one they asked for.

    This is the difference between authorised breadth and an accidental join: a
    document listing filtered by student must not spill another student's rows.
    """
    from tutortwin.domain.knowledge import SourceKind, Visibility
    from tutortwin.rag.embeddings import DeterministicEmbeddingProvider
    from tutortwin.rag.ingestion import IngestionRequest, ingest

    await make_admin(session, email="scope@example.com", role=AdminRole.SUPER_ADMIN)
    first = Subject(external_identity_type="test_phone", external_identity_value="+919990004444")
    second = Subject(external_identity_type="test_phone", external_identity_value="+919990005555")
    session.add_all([first, second])
    await session.commit()

    for subject, title in ((first, "First Notes"), (second, "Second Notes")):
        await ingest(
            session,
            IngestionRequest(
                title=title,
                text=f"Content belonging to {title}.",
                kind=SourceKind.NOTE,
                visibility=Visibility.STUDENT_PRIVATE,
                subject_id=subject.id,
            ),
            DeterministicEmbeddingProvider(),
        )
    await session.commit()

    token, _ = await sign_in(client, "scope@example.com")
    response = await client.get(
        "/v1/admin/documents", headers=auth(token), params={"student_id": str(first.id)}
    )
    assert response.status_code == 200
    titles = {item["title"] for item in response.json()["items"]}
    assert titles == {"First Notes"}


async def test_document_detail_returns_text_not_vectors(
    client: AsyncClient, session: AsyncSession
) -> None:
    from tutortwin.domain.knowledge import SourceKind, Visibility
    from tutortwin.rag.embeddings import DeterministicEmbeddingProvider
    from tutortwin.rag.ingestion import IngestionRequest, ingest

    await make_admin(session, email="vec@example.com", role=AdminRole.SUPER_ADMIN)
    subject = Subject(external_identity_type="test_phone", external_identity_value="+919990006666")
    session.add(subject)
    await session.commit()

    result = await ingest(
        session,
        IngestionRequest(
            title="Vector Check",
            text="Photosynthesis converts light energy into chemical energy.",
            kind=SourceKind.NOTE,
            visibility=Visibility.STUDENT_PRIVATE,
            subject_id=subject.id,
        ),
        DeterministicEmbeddingProvider(),
    )
    await session.commit()

    token, _ = await sign_in(client, "vec@example.com")
    response = await client.get(f"/v1/admin/documents/{result.source_id}", headers=auth(token))
    assert response.status_code == 200
    chunk = response.json()["chunks"][0]
    assert "text_preview" in chunk
    assert chunk["has_embedding"] is True
    assert "embedding_json" not in chunk, "a raw vector is unreadable and is not diagnostic"


# --- 10. job actions ----------------------------------------------------------


async def test_running_job_cannot_be_cancelled(client: AsyncClient, session: AsyncSession) -> None:
    """Marking an in-flight job cancelled produces a row that disagrees with reality."""
    from tutortwin.db.models import Job

    await make_admin(session, email="jobs@example.com", role=AdminRole.SUPPORT)
    job = Job(
        job_type="MEDIA_EXTRACT",
        state="RUNNING",
        idempotency_key="job-running-1",
        attempts=1,
    )
    session.add(job)
    await session.commit()

    token, csrf = await sign_in(client, "jobs@example.com")
    response = await client.post(
        f"/v1/admin/jobs/{job.id}/cancel",
        headers=auth(token, csrf),
        json={"reason": "trying to cancel an in-flight job"},
    )
    assert response.status_code == 409


async def test_retry_rearms_without_erasing_the_attempt_history(
    client: AsyncClient, session: AsyncSession
) -> None:
    from tutortwin.db.models import Job

    await make_admin(session, email="retry@example.com", role=AdminRole.SUPPORT)
    job = Job(
        job_type="MEDIA_EXTRACT",
        state="FAILED",
        idempotency_key="job-failed-1",
        attempts=3,
        max_attempts=3,
        last_error="provider timeout",
    )
    session.add(job)
    await session.commit()

    token, csrf = await sign_in(client, "retry@example.com")
    response = await client.post(
        f"/v1/admin/jobs/{job.id}/retry",
        headers=auth(token, csrf),
        json={"reason": "provider recovered, retrying"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "PENDING"
    assert body["attempts"] == 3, "history is kept"
    assert body["max_attempts"] == 4, "one more attempt is granted explicitly"

    event = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "JOB_RETRIED"))
    ).scalar_one()
    assert event.detail_json["before_state"] == "FAILED"


# --- 11. permission coverage --------------------------------------------------


def test_every_permission_belongs_to_at_least_one_role() -> None:
    """A permission no role holds is an endpoint nobody can ever call."""
    granted: set[Permission] = set()
    for role in AdminRole:
        granted |= set(permissions_for(role))
    assert granted == set(Permission)


def test_no_role_other_than_super_admin_can_grant_authority() -> None:
    for role in AdminRole:
        if role is AdminRole.SUPER_ADMIN:
            continue
        assert Permission.ADMIN_USER_WRITE not in permissions_for(role), role
