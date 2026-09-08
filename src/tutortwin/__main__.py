"""Service entrypoint: `python -m tutortwin`.

Cloud Run sends SIGTERM before it stops an instance. uvicorn handles that
signal, stops accepting connections and drains what is in flight, bounded by
`shutdown_grace_seconds` - which must be no larger than the platform's own
termination grace period, or the drain is cut off mid-request anyway.


Why this exists rather than invoking the `uvicorn` CLI: uvicorn creates its own
event loop when it starts, which on Windows is the ProactorEventLoop. Async
psycopg cannot run on that loop, so the API cannot reach Postgres. Setting a
policy at import time is not enough because uvicorn's own setup runs later; the
server has to be driven inside a loop we create.

On Linux (Cloud Run) `configure_event_loop_policy` is a no-op and this is simply
the entrypoint.
"""

from __future__ import annotations

import asyncio
import os

import uvicorn

from tutortwin.config import get_settings
from tutortwin.runtime import configure_event_loop_policy


def build_server() -> uvicorn.Server:
    settings = get_settings()
    config = uvicorn.Config(
        "tutortwin.api.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 - container binds all interfaces
        port=int(os.environ.get("PORT", "8080")),
        # Single worker: Cloud Run scales by instance, and extra workers would
        # multiply this container's Postgres connection count.
        workers=1,
        # "none" leaves the loop we installed alone instead of replacing it.
        loop="none",
        access_log=True,
        # Graceful shutdown. Cloud Run sends SIGTERM and then waits; uvicorn
        # stops accepting new connections and lets in-flight requests finish.
        # Without a bound, one hung request holds the container open until the
        # platform kills it - and a killed container is where a half-written
        # conversation comes from.
        timeout_graceful_shutdown=settings.shutdown_grace_seconds,
        # A request that has not finished by now never will. Bounding it here
        # means the socket is released rather than held by a stuck provider call.
        timeout_keep_alive=5,
    )
    return uvicorn.Server(config)


def main() -> None:
    configure_event_loop_policy()
    asyncio.run(build_server().serve())


if __name__ == "__main__":
    main()
