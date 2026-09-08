"""phase 06 usage ledger capability attribution

Revision ID: c4a17be9d520
Revises: a6e36c18df31
Create Date: 2026-09-03 00:00:00.000000

Cost by capability was the one grouping the control plane could not answer:
`usage_ledger` knew the model and the student but not what the call was *for*.
Deriving it after the fact meant guessing which message belonged to which
provider call, so the attribution is recorded at write time instead.

Nullable, and left null for every historical row. Backfilling a guess would make
old numbers look authoritative when they are not.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a17be9d520"
down_revision: str | None = "a6e36c18df31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("usage_ledger", sa.Column("capability", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_usage_capability_created", "usage_ledger", ["capability", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_usage_capability_created", table_name="usage_ledger")
    op.drop_column("usage_ledger", "capability")
