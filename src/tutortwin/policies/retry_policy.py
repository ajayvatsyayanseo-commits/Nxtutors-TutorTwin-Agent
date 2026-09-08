"""Retry policy, by job type.

Two ceilings, deliberately duplicated:

- **Cloud Tasks** retries a failed push on its own schedule.
- **`jobs.max_attempts`** bounds the same work in the database.

The duplication is the point. A queue misconfiguration - a hand-edited retry
config, a queue recreated with defaults - cannot produce an unbounded retry storm
against a paid provider, because the row itself refuses after N attempts. The
row is also what survives a queue being drained and rebuilt.

Backoff is exponential with a cap and full jitter. Without jitter, a provider
outage that fails a hundred jobs at once retries all hundred at the same instant,
which is a self-inflicted thundering herd against a vendor that is already sick.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: int
    max_delay_seconds: int

    def delay_for(self, attempt: int, *, jitter: bool = True) -> int:
        """Delay before attempt number `attempt` (1-based).

        Full jitter: a uniform draw over [0, exponential], which spreads a
        synchronised failure across the whole window instead of stacking every
        retry on the same second.
        """
        if attempt <= 1:
            return 0
        exponential = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** (attempt - 2)),
        )
        if not jitter:
            return int(exponential)
        return int(random.uniform(0, exponential))  # noqa: S311 - backoff, not crypto

    def exhausted(self, attempts: int) -> bool:
        return attempts >= self.max_attempts


# Media extraction is expensive and mostly fails for reasons that do not heal:
# a corrupt PDF is corrupt on the third attempt too. Three attempts, widely
# spaced, is enough to ride out a transient fetch or provider blip.
MEDIA_EXTRACT = RetryPolicy(max_attempts=3, base_delay_seconds=30, max_delay_seconds=600)

# Retention is idempotent and nobody is waiting for it, so it can afford to be
# patient and to try more times.
RETENTION_SWEEP = RetryPolicy(max_attempts=5, base_delay_seconds=60, max_delay_seconds=3600)

DEFAULT = RetryPolicy(max_attempts=3, base_delay_seconds=30, max_delay_seconds=600)

_BY_TYPE: dict[str, RetryPolicy] = {
    "MEDIA_EXTRACT": MEDIA_EXTRACT,
    "RETENTION_SWEEP": RETENTION_SWEEP,
}


def for_job_type(job_type: str) -> RetryPolicy:
    """Unknown types get the conservative default rather than unlimited retries."""
    return _BY_TYPE.get(job_type, DEFAULT)


__all__ = ["DEFAULT", "MEDIA_EXTRACT", "RETENTION_SWEEP", "RetryPolicy", "for_job_type"]
