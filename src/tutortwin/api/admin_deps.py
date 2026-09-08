"""FastAPI dependencies for the admin API: session, CSRF, permissions.

**Authorisation is a dependency, not a convention.** A route declares
`Depends(require(Permission.MODEL_WRITE))` and cannot execute without it. There
is no code path that reads the role and decides for itself, because that is the
path that eventually forgets.

**CSRF is required on every unsafe method, with no exceptions.** The admin API is
reachable with a cookie, and one rule that always applies is safer than a rule
with a "server-to-server" carve-out that an attacker only has to find.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.config import get_settings
from tutortwin.db.engine import get_session_factory
from tutortwin.domain.admin import AdminActor, Permission
from tutortwin.domain.errors import ErrorCode, TutorTwinError
from tutortwin.observability.logging import get_logger
from tutortwin.security.admin_auth import (
    AdminAuthError,
    ResolvedSession,
    csrf_matches,
    resolve_session,
)

logger = get_logger(__name__)

SESSION_COOKIE = "tt_admin_session"
SESSION_HEADER = "x-admin-session"
CSRF_HEADER = "x-admin-csrf"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def admin_session_db() -> AsyncIterator[AsyncSession]:
    """A session for admin requests, committed by the route that mutates."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


DbSession = Annotated[AsyncSession, Depends(admin_session_db)]


def _presented_token(request: Request) -> str | None:
    """Header first, then cookie.

    The header path is what the Next.js server uses; the cookie path is what a
    browser hitting the API directly would carry. Both are subject to the same
    CSRF requirement below.
    """
    header = request.headers.get(SESSION_HEADER)
    if header:
        return header
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE)


async def current_session(request: Request, db: DbSession) -> ResolvedSession:
    """Resolve and validate the session, then enforce CSRF on unsafe methods."""
    resolved = await resolve_session(db, _presented_token(request))

    if request.method.upper() not in _SAFE_METHODS and not csrf_matches(
        resolved.csrf_sha256, request.headers.get(CSRF_HEADER)
    ):
        logger.warning(
            "admin_csrf_rejected",
            method=request.method,
            path=request.url.path,
            admin_id=resolved.actor.admin_id,
        )
        raise TutorTwinError(ErrorCode.FORBIDDEN, "CSRF token missing or invalid.")

    return resolved


CurrentSession = Annotated[ResolvedSession, Depends(current_session)]


async def current_actor(session: CurrentSession) -> AdminActor:
    return session.actor


CurrentActor = Annotated[AdminActor, Depends(current_actor)]


def require(
    *permissions: Permission,
) -> Callable[[AdminActor], Coroutine[Any, Any, AdminActor]]:
    """Require every listed permission. Returns the actor so routes can audit it.

    Denials are logged with the permission that was missing, so an operator
    complaining "it says forbidden" is diagnosable without reproducing it.
    """

    async def dependency(actor: CurrentActor) -> AdminActor:
        missing = [p for p in permissions if not actor.can(p)]
        if missing:
            logger.warning(
                "admin_permission_denied",
                admin_id=actor.admin_id,
                role=str(actor.role),
                missing=[str(p) for p in missing],
            )
            # 403, not 404: the operator is authenticated, and pretending the
            # endpoint does not exist would make a permissions problem look like
            # a bug in the control plane.
            raise TutorTwinError(
                ErrorCode.FORBIDDEN,
                "Your role does not permit this action.",
            )
        return actor

    return dependency


async def require_password_current(session: CurrentSession) -> AdminActor:
    """Block ordinary work while a bootstrap password is still in place.

    The first password is printed to a terminal and possibly a log; treating it
    as a working credential indefinitely is how a bootstrap secret becomes a
    production one.
    """
    if session.must_change_password:
        raise TutorTwinError(
            ErrorCode.FORBIDDEN,
            "Password change required before using the control plane.",
        )
    return session.actor


def client_ip(request: Request) -> str | None:
    """The client address, as far as it can be trusted, for throttling and audit.

    **The leftmost `x-forwarded-for` entry is attacker-controlled.** It reads
    like "the original client", and taking it is the standard mistake: a
    password-spraying script sends a different fake IP on every attempt, every
    attempt looks like a first attempt from a new address, and the per-IP login
    throttle never fires. It is an unlimited-guesses bug wearing the costume of
    a rate limiter.

    Each proxy in the chain APPENDS what it saw, so the last entry was written
    by the proxy nearest us and cannot be forged by the client. With N trusted
    proxies the honest value is the Nth from the right; with none configured the
    header is ignored and the socket peer is used, which no client can spoof.
    """
    hops = get_settings().trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            chain = [part.strip() for part in forwarded.split(",") if part.strip()]
            if chain:
                # Nth from the right, clamped: a chain shorter than configured
                # means something changed in front of us, and walking past the
                # start would land back on a client-supplied value.
                index = max(0, len(chain) - hops)
                return chain[index][:64] or None
    return request.client.host[:64] if request.client else None


__all__ = [
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "SESSION_HEADER",
    "AdminAuthError",
    "CurrentActor",
    "CurrentSession",
    "DbSession",
    "admin_session_db",
    "client_ip",
    "current_actor",
    "current_session",
    "require",
    "require_password_current",
]
