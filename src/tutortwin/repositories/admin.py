"""Admin persistence: audit writing, account management, prompt versions.

**The audit event is written in the same transaction as the change it records.**
Not before, not after, not in a background task. A change that commits while its
audit row is lost is a change nobody can account for, and the two must succeed or
fail together.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.admin_models import AdminUser, PromptVersionRow
from tutortwin.db.models import AuditEvent
from tutortwin.domain.admin import AdminActor, AdminRole, HighRiskAction
from tutortwin.domain.errors import ErrorCode, TutorTwinError
from tutortwin.observability.logging import get_logger
from tutortwin.security.passwords import hash_password

logger = get_logger(__name__)

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25


def record_audit(
    session: AsyncSession,
    *,
    actor: AdminActor,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    reason: str | None = None,
    detail: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> None:
    """Stage an audit row. The caller's commit is what makes both durable.

    Deliberately not `async` and deliberately not committing: this must join the
    caller's transaction, and a signature that could commit on its own would
    invite exactly the split this design prevents.
    """
    payload: dict[str, Any] = dict(detail or {})
    if reason is not None:
        payload["reason"] = reason
    payload["actor_role"] = str(actor.role)

    session.add(
        AuditEvent(
            actor_type="ADMIN",
            actor_id=actor.admin_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            correlation_id=correlation_id,
            detail_json=payload,
        )
    )


def record_high_risk(
    session: AsyncSession,
    *,
    actor: AdminActor,
    action: HighRiskAction,
    target_type: str,
    target_id: str,
    reason: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> None:
    """Audit a dangerous change, with the before and after states attached.

    Recording only "someone changed the model route" leaves the reviewer unable
    to say what it was changed from, which is the question an incident actually
    asks.
    """
    record_audit(
        session,
        actor=actor,
        action=str(action),
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        detail={"high_risk": True, "before": before or {}, "after": after or {}},
        correlation_id=correlation_id,
    )
    logger.info(
        "admin_high_risk_action",
        action=str(action),
        actor_role=str(actor.role),
        target_type=target_type,
    )


def paginate(statement: Select[Any], *, page: int, page_size: int) -> Select[Any]:
    """Bounded server-side pagination. A caller cannot request the whole table."""
    size = max(1, min(page_size, MAX_PAGE_SIZE))
    offset = max(0, (max(1, page) - 1) * size)
    return statement.limit(size).offset(offset)


async def count_rows(session: AsyncSession, statement: Select[Any]) -> int:
    """Total for a filtered query, so the UI can show real page counts."""
    subquery = statement.order_by(None).subquery()
    return int((await session.execute(select(func.count()).select_from(subquery))).scalar_one())


# --- admin accounts -----------------------------------------------------------


async def list_admins(session: AsyncSession) -> list[AdminUser]:
    rows = await session.execute(select(AdminUser).order_by(AdminUser.email))
    return list(rows.scalars())


async def get_admin(session: AsyncSession, admin_id: str | UUID) -> AdminUser | None:
    return (
        await session.execute(select(AdminUser).where(AdminUser.id == admin_id))
    ).scalar_one_or_none()


async def count_admins(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(AdminUser))).scalar_one())


def normalise_email(email: str) -> str:
    """Validate and lower-case an administrator email.

    Applied here rather than only at the API boundary, so the CLI and the API
    agree on what an address is. Without it the bootstrap happily created
    `ops@nxtutors.local`, an account the login endpoint could never accept
    because `EmailStr` rejects reserved TLDs - measured, and the account was
    unusable from the moment it existed.
    """
    from email_validator import EmailNotValidError, validate_email

    try:
        # Deliverability is a network lookup and a source of flaky failures;
        # syntax and special-use checks are what actually matter here.
        result = validate_email(email.strip(), check_deliverability=False)
    except EmailNotValidError as exc:
        raise TutorTwinError(ErrorCode.VALIDATION_FAILED, f"Invalid email: {exc}") from exc
    return result.normalized.lower()


async def create_admin(
    session: AsyncSession,
    *,
    email: str,
    role: AdminRole,
    password: str | None = None,
    password_hash: str | None = None,
    display_name: str = "",
    must_change_password: bool = True,
) -> AdminUser:
    """Create an administrator from either a password or a ready-made hash.

    `password_hash` exists so a deployment can put an Argon2id PHC string in its
    environment instead of a plaintext password. A `.env` file is read by every
    process on the machine and leaks into shell history, backups and screen
    shares; a hash there is worthless to whoever reads it, which is the whole
    reason passwords are hashed at all.

    The hash is verified to be Argon2id before it is stored. Accepting an
    arbitrary string would let a typo become an account nobody can ever sign in
    to, discovered only at the worst moment.
    """
    if (password is None) == (password_hash is None):
        raise ValueError("Supply exactly one of password or password_hash.")

    normalised = normalise_email(email)
    existing = (
        await session.execute(select(AdminUser.id).where(AdminUser.email == normalised))
    ).scalar_one_or_none()
    if existing is not None:
        raise TutorTwinError(ErrorCode.CONFLICT, "An administrator with that email exists.")

    if password_hash is not None:
        if not password_hash.startswith("$argon2id$"):
            raise TutorTwinError(
                ErrorCode.VALIDATION_FAILED,
                "Password hash must be an Argon2id PHC string starting with $argon2id$.",
            )
        stored = password_hash
    else:
        assert password is not None
        stored = hash_password(password)

    user = AdminUser(
        email=normalised,
        display_name=display_name[:160],
        password_hash=stored,
        role=str(role),
        status="ACTIVE",
        must_change_password=must_change_password,
    )
    session.add(user)
    await session.flush()
    return user


# --- prompt versions ----------------------------------------------------------


async def next_prompt_version(session: AsyncSession, block_key: str) -> int:
    current = (
        await session.execute(
            select(func.max(PromptVersionRow.version)).where(
                PromptVersionRow.block_key == block_key
            )
        )
    ).scalar_one_or_none()
    return int(current or 0) + 1


async def activate_prompt_version(
    session: AsyncSession, *, version_id: UUID | str
) -> PromptVersionRow:
    """Activate one version and retire whichever was active for that block.

    Retiring rather than deleting: an answer given last week must remain
    explicable by the prompt that produced it.
    """
    target = (
        await session.execute(select(PromptVersionRow).where(PromptVersionRow.id == version_id))
    ).scalar_one_or_none()
    if target is None:
        raise TutorTwinError(ErrorCode.NOT_FOUND, "Prompt version not found.")

    live = (
        await session.execute(
            select(PromptVersionRow).where(
                PromptVersionRow.block_key == target.block_key,
                PromptVersionRow.status == "ACTIVE",
                PromptVersionRow.id != target.id,
            )
        )
    ).scalars()
    for row in live:
        row.status = "RETIRED"

    target.status = "ACTIVE"
    target.activated_at = datetime.now(UTC)
    await session.flush()
    return target


# --- shared time helpers ------------------------------------------------------


def window_start(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=max(1, days))


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "activate_prompt_version",
    "count_admins",
    "count_rows",
    "create_admin",
    "get_admin",
    "list_admins",
    "next_prompt_version",
    "normalise_email",
    "paginate",
    "record_audit",
    "record_high_risk",
    "window_start",
]
