"""phase 02 idempotency claim tracking + model catalog seed

Revision ID: 71e302352594
Revises: f80deb6c45fc
Create Date: 2026-08-31

Two changes:

1. `idempotency_keys` gains `claimed_at` / `completed_at` so an abandoned claim
   (container died mid-request) can be distinguished from a finished response and
   retried, instead of replaying an empty success to the student forever.
2. `model_catalog` is seeded so aliases resolve to vendor model IDs from the
   database. Vendor model strings live here and in the provider registry only -
   never in business code.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "71e302352594"
down_revision: str | None = "f80deb6c45fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (alias, provider, model_id, input_micros_per_1k, output_micros_per_1k)
_CATALOG_SEED: tuple[tuple[str, str, str, int, int], ...] = (
    ("CHEAP_TEXT", "anthropic", "claude-haiku-4-5", 1000, 5000),
    ("STANDARD_TUTOR", "anthropic", "claude-sonnet-5", 2000, 10000),
    ("ADVANCED_REASONING", "anthropic", "claude-opus-5", 5000, 25000),
    ("VERIFIER_PRIMARY", "anthropic", "claude-sonnet-5", 2000, 10000),
    ("VERIFIER_SECONDARY", "anthropic", "claude-haiku-4-5", 1000, 5000),
    ("CHEAP_TEXT", "openai", "gpt-4.1-mini", 400, 1600),
    ("STANDARD_TUTOR", "openai", "gpt-4.1", 2000, 8000),
    ("ADVANCED_REASONING", "openai", "o4-mini", 1100, 4400),
    ("VERIFIER_PRIMARY", "openai", "gpt-4.1", 2000, 8000),
    ("VERIFIER_SECONDARY", "openai", "gpt-4.1-mini", 400, 1600),
)

RATE_VERSION = "2026-08"


def upgrade() -> None:
    op.add_column(
        "idempotency_keys",
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "idempotency_keys",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfill: any pre-existing row that already holds a response is complete.
    # Without this, historical rows would look like in-flight claims and be
    # re-executed on the next duplicate.
    op.execute(
        """
        UPDATE idempotency_keys
           SET completed_at = created_at
         WHERE response_json IS NOT NULL
           AND response_json <> '{}'::jsonb
        """
    )

    for alias, provider, model_id, cost_in, cost_out in _CATALOG_SEED:
        op.execute(
            sa.text(
                """
                INSERT INTO model_catalog (
                    id, model_alias, provider, model_id, is_active,
                    input_cost_micros_per_1k, output_cost_micros_per_1k, rate_version
                )
                VALUES (
                    gen_random_uuid(), :alias, :provider, :model_id, true,
                    :cost_in, :cost_out, :rate_version
                )
                ON CONFLICT ON CONSTRAINT uq_model_catalog DO NOTHING
                """
            ).bindparams(
                alias=alias,
                provider=provider,
                model_id=model_id,
                cost_in=cost_in,
                cost_out=cost_out,
                rate_version=RATE_VERSION,
            )
        )

    # AI on by default; an operator flips this to fail closed on all paid calls.
    op.execute(
        sa.text(
            """
            INSERT INTO feature_flags (key, enabled, description)
            VALUES ('ai_enabled', true, 'Global kill switch for all paid AI calls.')
            ON CONFLICT (key) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM model_catalog WHERE rate_version = :v").bindparams(v=RATE_VERSION)
    )
    op.execute("DELETE FROM feature_flags WHERE key = 'ai_enabled'")
    op.drop_column("idempotency_keys", "completed_at")
    op.drop_column("idempotency_keys", "claimed_at")
