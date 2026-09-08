"""Liveness and readiness.

`/healthz` answers "is this process alive" and must never touch a dependency -
if it did, a database blip would cause the orchestrator to kill healthy containers.

`/readyz` answers "can this container serve traffic" and does check the database,
under a short timeout so a hung dependency fails fast instead of piling up.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from tutortwin.api.dependencies import Container, get_container
from tutortwin.db.engine import get_session_factory
from tutortwin.observability.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(container: Container = Depends(get_container)) -> JSONResponse:
    checks: dict[str, str] = {}
    ready = True

    try:
        async with asyncio.timeout(container.settings.db_health_timeout_seconds):
            factory = get_session_factory()
            async with factory() as session:
                await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except TimeoutError:
        checks["database"] = "timeout"
        ready = False
    except Exception as exc:
        logger.warning("readiness_db_failed", error_type=type(exc).__name__)
        checks["database"] = "error"
        ready = False

    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )
