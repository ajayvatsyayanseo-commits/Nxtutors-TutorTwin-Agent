"""Admin identity, sessions and login-attempt tracking.

**Only hashes are stored — of passwords and of session tokens alike.** A stolen
database dump must not yield a working session any more than it yields a working
password, so the session token is generated once, returned once, and kept as a
SHA-256. A read of `admin_sessions` cannot mint a session from what it finds.

Session tokens use SHA-256 rather than Argon2 deliberately: they are 256 bits of
CSPRNG output, so there is nothing to brute-force, and a per-request Argon2
verification would add ~75 ms to every admin page load.

**Login attempts live in a table, not in memory.** The API runs as many
short-lived containers; an in-process rate limiter resets on every cold start,
which means it does not limit anything.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tutortwin.db.models import Base


class AdminUser(Base):
    """An operator of the control plane."""

    __tablename__ = "admin_users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")

    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    """Argon2id, PHC string format. The parameters travel inside the hash, so
    raising them later re-hashes on next login instead of invalidating everyone."""

    role: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")

    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Set by the bootstrap procedure. The generated first password is a delivery
    mechanism, not a credential meant to persist."""

    totp_secret_encrypted: Mapped[str | None] = mapped_column(String(512))
    """Reserved for TOTP. Nullable and unused in this phase - the column exists so
    enabling second factor is a migration of behaviour, not of schema."""

    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("email", name="uq_admin_email"),
        CheckConstraint(
            "role IN ('SUPER_ADMIN', 'ADMIN', 'ACADEMIC_ADMIN', 'SUPPORT',"
            " 'TUTOR_VIEWER', 'USAGE_VIEWER')",
            name="ck_admin_role",
        ),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_admin_status"),
        Index("ix_admin_status", "status"),
    )


class AdminSession(Base):
    """One logged-in browser. Revocable, expiring, and never stored in the clear."""

    __tablename__ = "admin_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    admin_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )

    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    """A second, independent secret. The session cookie authenticates the browser;
    this proves the request came from our own page rather than someone else's."""

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))

    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(256))

    __table_args__ = (
        UniqueConstraint("token_sha256", name="uq_session_token"),
        Index("ix_session_user_active", "admin_user_id", "revoked_at"),
        Index("ix_session_expiry", "expires_at"),
    )


class AdminLoginAttempt(Base):
    """Append-only login history, used for rate limiting and for the audit trail.

    Both the email and the client address are recorded so the limiter can bound
    two different attacks: many passwords against one account, and one password
    against many accounts.
    """

    __tablename__ = "admin_login_attempts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    successful: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_reason: Mapped[str | None] = mapped_column(String(48))
    """A coarse code (`bad_password`, `unknown_user`, `locked`, `disabled`). The
    login response never distinguishes these; only this table does."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_login_attempt_email_time", "email", "created_at"),
        Index("ix_login_attempt_ip_time", "ip_address", "created_at"),
    )


class PromptVersionRow(Base):
    """Versioned prompt blocks, editable from the control plane.

    Phase 02 keeps prompt text in code; this table is the operator-editable
    overlay. An activated version is immutable - a change creates a new row - so
    an answer given last week can still be explained by the prompt that produced
    it.
    """

    __tablename__ = "prompt_versions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    block_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(String(8000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    author_admin_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None] = mapped_column(String(500))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("block_key", "version", name="uq_prompt_block_version"),
        CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'RETIRED')",
            name="ck_prompt_status",
        ),
        Index("ix_prompt_active", "block_key", "status"),
    )


__all__ = ["AdminLoginAttempt", "AdminSession", "AdminUser", "PromptVersionRow"]
