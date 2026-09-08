"""Event-loop policy.

Async psycopg cannot run on Windows' default ProactorEventLoop; it needs a
selector loop. Production is Linux/Cloud Run where this is a no-op, but local
development and CI on Windows break without it, so the fix lives in the runtime
rather than in test setup.
"""

from __future__ import annotations

import asyncio
import sys


def configure_event_loop_policy() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
