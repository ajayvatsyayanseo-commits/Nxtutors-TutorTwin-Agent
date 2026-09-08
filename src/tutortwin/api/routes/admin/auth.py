"""Admin authentication and administrator management.

The login response carries the CSRF token in its **body**, not in a cookie. The
Next.js server keeps it server-side and echoes it as a header on mutations; a
cross-site page can cause the cookie to be sent but cannot read a body it never
received.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from tutortwin.api.admin_deps import (
    CSRF_HEADER,
    SESSION_COOKIE,
    CurrentActor,
    CurrentSession,
    DbSession,
    client_ip,
    require,
)
from tutortwin.config import get_settings
from tutortwin.domain.admin import (
    AdminActor,
    AdminRole,
    HighRiskAction,
    HighRiskRequest,
    Permission,
    permissions_for,
)
from tutortwin.domain.errors import ErrorCode, TutorTwinError
from tutortwin.observability.logging import get_logger
from tutortwin.repositories import admin as admin_repo
from tutortwin.security import admin_auth
from tutortwin.security.passwords import MIN_PASSWORD_CHARS, WeakPassword, hash_password

router = APIRouter()
logger = get_logger(__name__)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class ActorView(BaseModel):
    admin_id: str
    email: str
    role: AdminRole
    permissions: list[str]
    must_change_password: bool


class LoginResponse(BaseModel):
    actor: ActorView
    csrf_token: str
    expires_at: datetime


def _actor_view(actor: AdminActor, *, must_change_password: bool) -> ActorView:
    return ActorView(
        admin_id=actor.admin_id,
        email=actor.email,
        role=actor.role,
        permissions=sorted(str(p) for p in actor.permissions),
        must_change_password=must_change_password,
    )


@router.post("/auth/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest, request: Request, response: Response, db: DbSession
) -> LoginResponse:
    issued = await admin_auth.login(
        db,
        email=str(payload.email),
        password=payload.password,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        issued.session_token,
        httponly=True,
        # `secure` off outside production only so http://localhost works; the
        # flag is on wherever it matters.
        secure=settings.is_production,
        samesite="strict",
        expires=issued.expires_at,
        path="/",
    )
    return LoginResponse(
        actor=_actor_view(issued.actor, must_change_password=issued.must_change_password),
        csrf_token=issued.csrf_token,
        expires_at=issued.expires_at,
    )


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response, db: DbSession) -> None:
    """Revoke server-side, then clear the cookie.

    Order matters: clearing only the cookie would leave a live session that a
    stolen token could still use.
    """
    from tutortwin.api.admin_deps import SESSION_HEADER

    token = request.headers.get(SESSION_HEADER) or request.cookies.get(SESSION_COOKIE) or None
    await admin_auth.logout(db, token)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/auth/me", response_model=ActorView)
async def me(session: CurrentSession) -> ActorView:
    return _actor_view(session.actor, must_change_password=session.must_change_password)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD_CHARS, max_length=256)


@router.post("/auth/change-password", status_code=204)
async def change_password(
    payload: ChangePasswordRequest, actor: CurrentActor, db: DbSession
) -> None:
    """Change your own password. Every session for the account is then revoked."""
    if payload.new_password == payload.current_password:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "New password must be different.")
    try:
        await admin_auth.change_password(
            db,
            admin_user_id=actor.admin_id,
            current=payload.current_password,
            new=payload.new_password,
        )
    except WeakPassword as exc:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc

    admin_repo.record_audit(
        db,
        actor=actor,
        action="ADMIN_PASSWORD_CHANGED",
        target_type="admin_user",
        target_id=actor.admin_id,
    )
    await db.commit()


# --- administrator management (SUPER_ADMIN only) ------------------------------


class AdminSummary(BaseModel):
    id: str
    email: str
    display_name: str
    role: AdminRole
    status: str
    must_change_password: bool
    last_login_at: datetime | None
    created_at: datetime


@router.get("/admins", response_model=list[AdminSummary])
async def list_admins(
    db: DbSession,
    _: AdminActor = Depends(require(Permission.ADMIN_USER_READ)),
) -> list[AdminSummary]:
    rows = await admin_repo.list_admins(db)
    return [
        AdminSummary(
            id=str(row.id),
            email=row.email,
            display_name=row.display_name,
            role=AdminRole(row.role),
            status=row.status,
            must_change_password=row.must_change_password,
            last_login_at=row.last_login_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


class CreateAdminRequest(HighRiskRequest):
    email: EmailStr
    display_name: str = Field(default="", max_length=160)
    role: AdminRole
    password: str = Field(min_length=MIN_PASSWORD_CHARS, max_length=256)


@router.post("/admins", response_model=AdminSummary, status_code=201)
async def create_admin(
    payload: CreateAdminRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.ADMIN_USER_WRITE)),
) -> AdminSummary:
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")
    try:
        created = await admin_repo.create_admin(
            db,
            email=str(payload.email),
            password=payload.password,
            role=payload.role,
            display_name=payload.display_name,
            must_change_password=True,
        )
    except WeakPassword as exc:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.ADMIN_ROLE_CHANGE,
        target_type="admin_user",
        target_id=str(created.id),
        reason=payload.reason,
        after={"email": created.email, "role": created.role},
    )
    await db.flush()
    view = AdminSummary(
        id=str(created.id),
        email=created.email,
        display_name=created.display_name,
        role=AdminRole(created.role),
        status=created.status,
        must_change_password=created.must_change_password,
        last_login_at=None,
        created_at=created.created_at,
    )
    await db.commit()
    return view


class ChangeRoleRequest(HighRiskRequest):
    role: AdminRole


@router.post("/admins/{admin_id}/role", response_model=AdminSummary)
async def change_role(
    admin_id: str,
    payload: ChangeRoleRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.ADMIN_USER_WRITE)),
) -> AdminSummary:
    """Change an administrator's role, then revoke their sessions.

    A role change that leaves the old session working has not taken effect - the
    permission set is read from the session's user row on every request, but
    revoking makes the change visible to the operator immediately rather than
    silently mid-click.
    """
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")

    target = await admin_repo.get_admin(db, admin_id)
    if target is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Administrator not found.")

    # Self-demotion is refused rather than allowed and regretted: an account that
    # removes its own last SUPER_ADMIN authority can lock the whole team out.
    if str(target.id) == actor.admin_id and payload.role is not AdminRole.SUPER_ADMIN:
        raise TutorTwinError(
            ErrorCode.FORBIDDEN, "You cannot reduce your own role. Ask another super admin."
        )

    before = target.role
    target.role = str(payload.role)
    revoked = await admin_auth.revoke_all_for_user(
        db, admin_user_id=str(target.id), reason="role_change"
    )

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.ADMIN_ROLE_CHANGE,
        target_type="admin_user",
        target_id=str(target.id),
        reason=payload.reason,
        before={"role": before},
        after={"role": target.role, "sessions_revoked": revoked},
    )
    await db.flush()
    view = AdminSummary(
        id=str(target.id),
        email=target.email,
        display_name=target.display_name,
        role=AdminRole(target.role),
        status=target.status,
        must_change_password=target.must_change_password,
        last_login_at=target.last_login_at,
        created_at=target.created_at,
    )
    await db.commit()
    return view


class SetStatusRequest(HighRiskRequest):
    status: str = Field(pattern="^(ACTIVE|DISABLED)$")


@router.post("/admins/{admin_id}/status", status_code=204)
async def set_status(
    admin_id: str,
    payload: SetStatusRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.ADMIN_USER_WRITE)),
) -> None:
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")
    if admin_id == actor.admin_id:
        raise TutorTwinError(ErrorCode.FORBIDDEN, "You cannot disable your own account.")

    target = await admin_repo.get_admin(db, admin_id)
    if target is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Administrator not found.")

    before = target.status
    target.status = payload.status
    if payload.status == "DISABLED":
        await admin_auth.revoke_all_for_user(
            db, admin_user_id=str(target.id), reason="account_disabled"
        )

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.ADMIN_ROLE_CHANGE,
        target_type="admin_user",
        target_id=str(target.id),
        reason=payload.reason,
        before={"status": before},
        after={"status": target.status},
    )
    await db.commit()


class ResetPasswordRequest(HighRiskRequest):
    new_password: str = Field(min_length=MIN_PASSWORD_CHARS, max_length=256)


@router.post("/admins/{admin_id}/reset-password", status_code=204)
async def reset_password(
    admin_id: str,
    payload: ResetPasswordRequest,
    db: DbSession,
    actor: AdminActor = Depends(require(Permission.ADMIN_USER_WRITE)),
) -> None:
    """Administrative reset. The target must change it at next login."""
    if not payload.confirm:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, "Confirmation required.")

    target = await admin_repo.get_admin(db, admin_id)
    if target is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Administrator not found.")

    try:
        target.password_hash = hash_password(payload.new_password)
    except WeakPassword as exc:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc

    target.must_change_password = True
    target.failed_attempts = 0
    target.locked_until = None
    await admin_auth.revoke_all_for_user(db, admin_user_id=str(target.id), reason="password_reset")

    admin_repo.record_high_risk(
        db,
        actor=actor,
        action=HighRiskAction.ADMIN_ROLE_CHANGE,
        target_type="admin_user",
        target_id=str(target.id),
        reason=payload.reason,
        after={"password_reset": True},
    )
    await db.commit()


@router.get("/roles", response_model=dict[str, list[str]])
async def role_matrix(_: CurrentActor) -> dict[str, list[str]]:
    """The RBAC matrix, so the UI shows the same rules the server enforces."""
    return {str(role): sorted(str(p) for p in permissions_for(role)) for role in AdminRole}


__all__ = ["CSRF_HEADER", "router"]
