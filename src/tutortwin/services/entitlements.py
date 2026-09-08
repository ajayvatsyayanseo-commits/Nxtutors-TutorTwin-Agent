"""Entitlement, read from the database instead of from a config list.

This is the keystone of the subscription feature, and it is deliberately small.

Everything that grants access already writes the `entitlements` table correctly:
the admin override endpoint supersedes the previous row and records a high-risk
audit entry, and payment activation writes the same shape. Nothing **read** that
table - the runtime resolved entitlement from `TUTORTWIN_FAKE_PRO_SUBJECTS`, a
static list in the environment. So a paid subscription and an operator's manual
grant both landed in a table nobody consulted, and the student stayed on FREE.

One adapter closes that. It is also the security boundary for spend: an
`EntitlementSnapshot` whose status is not ACTIVE means zero paid provider calls
for that student, so a bug here is either a student paying for nothing or a
non-subscriber spending your model budget.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutortwin.db.models import Entitlement
from tutortwin.domain.models import EntitlementSnapshot, EntitlementStatus, ResolvedSubject
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

FREE_PLAN = "FREE"

ACTIVE = "ACTIVE"
"""The only row status that grants access. `SUPERSEDED`, `CANCELLED` and
`EXPIRED` rows are kept rather than deleted, because the history of who had
what, when, is what a billing dispute is settled with."""


class DatabaseEntitlementGateway:
    """Reads the newest ACTIVE entitlement row for a subject.

    Never calls a paid provider, per the port's contract - it is the thing that
    decides whether a paid provider may be called at all.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def snapshot(self, subject: ResolvedSubject) -> EntitlementSnapshot:
        now = self._now()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(Entitlement)
                    .where(Entitlement.subject_id == subject.id, Entitlement.status == ACTIVE)
                    # Newest first. Two ACTIVE rows should be impossible - the
                    # writers supersede - but if it ever happens, honouring the
                    # most recent decision is the least surprising behaviour,
                    # and far better than an arbitrary one.
                    .order_by(Entitlement.fetched_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        if row is None:
            return EntitlementSnapshot(
                subject_id=subject.id,
                plan_code=FREE_PLAN,
                status=EntitlementStatus.INACTIVE,
                fetched_at=now,
                source="database",
            )

        # Expiry is enforced on read, not by a sweeper. A subscription that
        # lapsed at midnight must stop granting access at midnight, even if no
        # job has run since - and a sweeper that fails silently would otherwise
        # keep handing out a plan the student stopped paying for.
        expired = row.ends_at is not None and row.ends_at <= now
        if expired:
            logger.info(
                "entitlement_expired",
                subject_id=str(subject.id),
                plan_code=row.plan_code,
                ended_at=row.ends_at.isoformat() if row.ends_at else None,
            )

        return EntitlementSnapshot(
            subject_id=subject.id,
            plan_code=FREE_PLAN if expired else row.plan_code,
            status=EntitlementStatus.INACTIVE if expired else EntitlementStatus.ACTIVE,
            fetched_at=now,
            source=row.source,
            ends_at=row.ends_at,
        )


__all__ = ["ACTIVE", "FREE_PLAN", "DatabaseEntitlementGateway"]
