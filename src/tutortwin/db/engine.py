"""Async engine and session factory tuned for serverless (Cloud Run + Neon).

Serverless correctness rules encoded here:

* Small pool. Many short-lived containers each holding a large pool exhausts
  Postgres. Default is 2 + 2 overflow.
* `pool_pre_ping` - a container can be frozen between requests and wake with a
  dead socket; without this the first query after a cold resume fails.
* `pool_recycle` below Neon's idle timeout so we never hand out a stale socket.
* Explicit connect timeout, and a server-side `statement_timeout` applied per
  connection so a pathological query cannot pin a request.
* NullPool for migrations: Alembic runs once and exits, pooling buys nothing and
  a pooled endpoint breaks DDL advisory locks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tutortwin.config import Settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.app_dsn,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle_seconds,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            # Server-side ceiling on any single statement, plus the schema this
            # deployment owns. On a shared database the search_path is what keeps
            # every TutorTwin table inside its own schema and every other
            # product's table invisible to us.
            "options": (
                f"-c statement_timeout={settings.db_statement_timeout_ms} "
                f"-c search_path={settings.search_path}"
            ),
        },
        echo=False,
    )


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = build_engine(settings)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Engine not initialised. Call init_engine() during startup.")
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work.

    Never wrap an LLM/network provider call in this scope - a provider call can
    take tens of seconds and would hold a Postgres transaction open the whole time.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
