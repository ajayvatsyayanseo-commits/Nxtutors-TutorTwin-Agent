"""Seed the VISION and TRANSCRIBE model aliases.

Without these rows the whole media half of the product is silently dead.

`ModelGateway` resolves an alias through `model_catalog`. The Phase 02 seed
listed only the five text aliases, so `VISION` and `TRANSCRIBE` resolved to
nothing: a photo that failed local OCR escalated to a vision model that could
not be looked up, a voice note reached a transcriber that did not exist, and
both returned empty. No error was raised - the media was marked
READY_FOR_CAPABILITY with no text, and the tutor answered a question it had
never seen.

That is invisible until a real student sends a photo, because every test either
stubs the gateway or runs with no provider configured at all.

TRANSCRIBE is OpenAI only. Anthropic has no speech-to-text, so a deployment
holding only an Anthropic key cannot hear voice notes - which is correct, and is
why the transcriber returns None rather than refusing to start.

Revision ID: 4d9f4d742e3c
Revises: f211de0a201a
Create Date: 2026-09-09 10:02:26.400427
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4d9f4d742e3c"
down_revision: str | None = "f211de0a201a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RATE_VERSION = "2026-08"

# (alias, provider, model_id, input micros/1k, output micros/1k)
#
# Vision is priced as the standard tutor tier because it is the same underlying
# model reading an image; the cost difference lives in the image tokens, which
# the gateway already counts as input.
#
# Transcription is billed by audio DURATION, not tokens, so both token rates are
# zero. The ledger row still records the call, which is what keeps a voice note
# visible in spend even though the token columns stay empty.
_SEED: tuple[tuple[str, str, str, int, int], ...] = (
    ("VISION", "anthropic", "claude-sonnet-5", 2000, 10000),
    ("VISION", "openai", "gpt-4.1", 2000, 8000),
    ("TRANSCRIBE", "openai", "whisper-1", 0, 0),
)


def upgrade() -> None:
    for alias, provider, model_id, cost_in, cost_out in _SEED:
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


def downgrade() -> None:
    # Scoped to the aliases this revision added, and to the rate version it
    # wrote, so an operator who has since re-priced these rows keeps their work.
    op.execute(
        sa.text(
            "DELETE FROM model_catalog "
            "WHERE model_alias IN ('VISION', 'TRANSCRIBE') AND rate_version = :rate"
        ).bindparams(rate=RATE_VERSION)
    )
