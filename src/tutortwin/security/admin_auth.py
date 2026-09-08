"""Admin login, sessions, CSRF and login rate limiting.

The whole file exists to make four statements true:

**A database dump does not yield a login.** Passwords are Argon2id; session and
CSRF tokens are 256-bit random values stored as SHA-256. Nothing here can be
replayed from what is stored.

**A failed login tells the attacker nothing.** Unknown email, wrong password,
disabled account and locked account all return the same message after the same
amount of work. Which one it actually was is recorded in `admin_login_attempts`,
where only an operator can read it.

**Rate limiting survives a cold start.** The counters are rows, not process
state; an in-memory limiter in a serverless deployment resets on every new
container and therefore limits nothing.

**A cookie alone cannot perform a mutation.** The session cookie proves *who*;
the CSRF token, delivered in the login response and echoed in a header, proves
the request came from our own page. A cross-site form post carries the cookie
but cannot read the token.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.admin_models import AdminLoginAttempt, AdminSession, AdminUser
from tutortwin.domain.admin import AdminActor, AdminRole
from tutortwin.domain.errors import ErrorCode, TutorTwinError
from tutortwin.observability.logging import get_logger
from tutortwin.security.passwords import (
    dummy_verify,
    hash_password,
    needs_rehash,
    verify_password,
)

logger = get_logger(__name__)

SESSION_TTL_HOURS = 12
"""One working day. Long enough that an operator is not re-authenticating all
afternoon, short enough that a forgotten browser is not a standing key."""

SESSION_IDLE_TIMEOUT_MINUTES = 60
"""An unattended session stops working before the absolute expiry does."""

TOKEN_BYTES = 32  # 256 bits

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
"""Locks the *account*, which is the resource being attacked. Long enough to
make online guessing pointless, short enough that a legitimate operator who
mistyped is not paged out of their own system."""

IP_ATTEMPT_WINDOW_MINUTES = 15
MAX_IP_ATTEMPTS = 20
"""Bounds password spraying: one guess each against many accounts never trips a
per-account lock, but does trip this."""

GENERIC_LOGIN_FAILURE = "Invalid email or password."
"""One message for every failure mode. Distinguishing them is a free account
enumeration oracle."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Returned once, at login. The raw tokens are never stored or logged."""

    session_token: str
    csrf_token: str
    expires_at: datetime
    actor: AdminActor
    must_change_password: bool


class AdminAuthError(TutorTwinError):
    def __init__(self, message: str = GENERIC_LOGIN_FAILURE) -> None:
        super().__init__(ErrorCode.UNAUTHENTICATED, message)


class RateLimited(TutorTwinError):
    def __init__(self, message: str = "Too many attempts. Try again later.") -> None:
        super().__init__(ErrorCode.RATE_LIMITED, message)


async def _record_attempt(
    session: AsyncSession,
    *,
    email: str,
    ip: str | None,
    successful: bool,
    reason: str | None,
) -> None:
    session.add(
        AdminLoginAttempt(
            email=email.lower()[:320],
            ip_address=ip,
            successful=successful,
            failure_reason=reason,
        )
    )


async def _ip_is_throttled(session: AsyncSession, ip: str | None) -> bool:
    if ip is None:
        return False
    since = _now() - timedelta(minutes=IP_ATTEMPT_WINDOW_MINUTES)
    count = (
        await session.execute(
            select(func.count())
            .select_from(AdminLoginAttempt)
            .where(
                AdminLoginAttempt.ip_address == ip,
                AdminLoginAttempt.successful.is_(False),
                AdminLoginAttempt.created_at >= since,
            )
        )
    ).scalar_one()
    return bool(count >= MAX_IP_ATTEMPTS)


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> IssuedSession:
    """Authenticate and issue a session. Constant-work on every failure path."""
    normalised = email.strip().lower()

    if await _ip_is_throttled(session, ip):
        await _record_attempt(
            session, email=normalised, ip=ip, successful=False, reason="ip_throttled"
        )
        await session.commit()
        logger.warning("admin_login_ip_throttled", ip=ip)
        raise RateLimited()

    user = (
        await session.execute(select(AdminUser).where(AdminUser.email == normalised))
    ).scalar_one_or_none()

    if user is None:
        # Same work, same message. The absence of an account is not disclosed.
        dummy_verify(password)
        await _record_attempt(
            session, email=normalised, ip=ip, successful=False, reason="unknown_user"
        )
        await session.commit()
        raise AdminAuthError()

    if user.status != "ACTIVE":
        dummy_verify(password)
        await _record_attempt(session, email=normalised, ip=ip, successful=False, reason="disabled")
        await session.commit()
        raise AdminAuthError()

    if user.locked_until is not None and user.locked_until > _now():
        dummy_verify(password)
        await _record_attempt(session, email=normalised, ip=ip, successful=False, reason="locked")
        await session.commit()
        raise AdminAuthError()

    if not verify_password(user.password_hash, password):
        user.failed_attempts += 1
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = _now() + timedelta(minutes=LOCKOUT_MINUTES)
            logger.warning("admin_account_locked", admin_id=str(user.id))
        await _record_attempt(
            session, email=normalised, ip=ip, successful=False, reason="bad_password"
        )
        await session.commit()
        raise AdminAuthError()

    # Correct password. Raise the cost silently if the stored parameters are old.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = _now()

    issued = await _issue_session(session, user=user, ip=ip, user_agent=user_agent)
    await _record_attempt(session, email=normalised, ip=ip, successful=True, reason=None)
    await session.commit()

    logger.info("admin_login", admin_id=str(user.id), role=user.role)
    return issued


async def _issue_session(
    session: AsyncSession, *, user: AdminUser, ip: str | None, user_agent: str | None
) -> IssuedSession:
    session_token = secrets.token_urlsafe(TOKEN_BYTES)
    csrf_token = secrets.token_urlsafe(TOKEN_BYTES)
    expires_at = _now() + timedelta(hours=SESSION_TTL_HOURS)

    row = AdminSession(
        admin_user_id=user.id,
        token_sha256=_sha256(session_token),
        csrf_sha256=_sha256(csrf_token),
        expires_at=expires_at,
        ip_address=ip,
        user_agent=(user_agent or "")[:256] or None,
    )
    session.add(row)
    await session.flush()

    return IssuedSession(
        session_token=session_token,
        csrf_token=csrf_token,
        expires_at=expires_at,
        actor=AdminActor(
            admin_id=str(user.id),
            email=user.email,
            role=AdminRole(user.role),
            session_id=str(row.id),
        ),
        must_change_password=user.must_change_password,
    )


@dataclass(frozen=True, slots=True)
class ResolvedSession:
    actor: AdminActor
    csrf_sha256: str
    must_change_password: bool


async def resolve_session(session: AsyncSession, token: str | None) -> ResolvedSession:
    """Verify a session token. Raises UNAUTHENTICATED for anything unusable.

    The join to `admin_users` is what makes a disabled account take effect
    immediately: revoking a role must not wait for a 12-hour session to expire.
    """
    if not token:
        raise AdminAuthError("Authentication required.")

    now = _now()
    row = (
        await session.execute(
            select(AdminSession, AdminUser)
            .join(AdminUser, AdminUser.id == AdminSession.admin_user_id)
            .where(AdminSession.token_sha256 == _sha256(token))
        )
    ).one_or_none()

    if row is None:
        raise AdminAuthError("Authentication required.")

    admin_session, user = row
    if admin_session.revoked_at is not None:
        raise AdminAuthError("Session has been revoked.")
    if admin_session.expires_at <= now:
        raise AdminAuthError("Session has expired.")
    if admin_session.last_seen_at + timedelta(minutes=SESSION_IDLE_TIMEOUT_MINUTES) <= now:
        # Idle timeout is enforced on read rather than by a sweeper, so it holds
        # even if no background job ever runs.
        admin_session.revoked_at = now
        admin_session.revoked_reason = "idle_timeout"
        await session.commit()
        raise AdminAuthError("Session has expired.")
    if user.status != "ACTIVE":
        raise AdminAuthError("Account is disabled.")

    admin_session.last_seen_at = now

    return ResolvedSession(
        actor=AdminActor(
            admin_id=str(user.id),
            email=user.email,
            role=AdminRole(user.role),
            session_id=str(admin_session.id),
        ),
        csrf_sha256=admin_session.csrf_sha256,
        must_change_password=user.must_change_password,
    )


def csrf_matches(expected_sha256: str, presented: str | None) -> bool:
    """Constant-time comparison of the hashed CSRF token."""
    if not presented:
        return False
    return secrets.compare_digest(expected_sha256, _sha256(presented))


async def logout(session: AsyncSession, token: str | None) -> None:
    """Revoke one session. Silent when the token is already unusable."""
    if not token:
        return
    await session.execute(
        update(AdminSession)
        .where(AdminSession.token_sha256 == _sha256(token), AdminSession.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_reason="logout")
    )
    await session.commit()


async def revoke_all_for_user(session: AsyncSession, *, admin_user_id: str, reason: str) -> int:
    """Revoke every live session for one administrator.

    Called when a role changes, an account is disabled, or a password is reset -
    a role change that leaves the old session working has not taken effect.
    """
    result = await session.execute(
        update(AdminSession)
        .where(
            AdminSession.admin_user_id == admin_user_id,
            AdminSession.revoked_at.is_(None),
        )
        .values(revoked_at=_now(), revoked_reason=reason[:64])
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def change_password(
    session: AsyncSession, *, admin_user_id: str, current: str, new: str
) -> None:
    """Change a password, then revoke every other session for that account."""
    user = (
        await session.execute(select(AdminUser).where(AdminUser.id == admin_user_id))
    ).scalar_one_or_none()
    if user is None:
        raise AdminAuthError("Authentication required.")
    if not verify_password(user.password_hash, current):
        raise AdminAuthError("Current password is incorrect.")

    user.password_hash = hash_password(new)
    user.must_change_password = False
    await revoke_all_for_user(session, admin_user_id=admin_user_id, reason="password_change")
    await session.commit()
    logger.info("admin_password_changed", admin_id=admin_user_id)


__all__ = [
    "GENERIC_LOGIN_FAILURE",
    "LOCKOUT_MINUTES",
    "MAX_FAILED_ATTEMPTS",
    "MAX_IP_ATTEMPTS",
    "SESSION_IDLE_TIMEOUT_MINUTES",
    "SESSION_TTL_HOURS",
    "AdminAuthError",
    "IssuedSession",
    "RateLimited",
    "ResolvedSession",
    "change_password",
    "csrf_matches",
    "login",
    "logout",
    "resolve_session",
    "revoke_all_for_user",
]
