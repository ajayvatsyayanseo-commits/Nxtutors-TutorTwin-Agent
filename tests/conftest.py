"""Shared fixtures.

Integration tests run against a real PostgreSQL database whose schema is built by
the real Alembic migrations (not `create_all`), so migration drift is caught by
the same tests that exercise behaviour.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tutortwin.runtime import configure_event_loop_policy

configure_event_loop_policy()

TEST_DSN = os.environ.get(
    "TUTORTWIN_TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin_test",
)

# The schema has to travel with the DSN.
#
# Tests build `Settings(database_url=TEST_DSN, ...)` and let every other field
# fall through to the developer's `.env`. `database_postgres_schema` is the one
# field where that is wrong: production points at a *shared* database and sets
# it to `tutor_twin`, while this test database is TutorTwin's alone and keeps
# its tables in `public`. Inheriting the production value describes a schema
# that does not exist here, and the boot-time isolation check correctly refuses
# to start against it.
#
# Set as a process env var rather than passed per-fixture because pydantic
# ranks the environment above `.env`, so this overrides the developer's file
# for every `Settings()` the suite constructs - including the ones inside
# application code that tests do not build themselves. Export it to point a run
# at a schema-isolated database instead.
os.environ.setdefault("TUTORTWIN_DATABASE_POSTGRES_SCHEMA", "public")

# The list is explicit so a new table added without a fixture update shows up as
# leaking state rather than silently polluting later tests.
#
# It is TRUNCATEd in sorted order (see below): a single TRUNCATE takes an
# ACCESS EXCLUSIVE lock on every named table, and two sessions naming them in
# different orders can deadlock against each other. Sorting gives every session
# the same lock order, which makes that impossible.
_TABLES = (
    "admin_login_attempts",
    "feature_flags",
    "model_catalog",
    "plan_policies",
    "admin_sessions",
    "admin_users",
    "prompt_versions",
    "attempt_responses",
    "assessment_attempts",
    "assessment_questions",
    "assessments",
    "flashcard_reviews",
    "flashcards",
    "flashcard_decks",
    "study_notes",
    "homework_tasks",
    "learning_artifacts",
    "retrieval_events",
    "document_chunks",
    "knowledge_sources",
    "embedding_cache",
    "student_memories",
    "topic_stats",
    "conversation_summaries",
    "media_extractions",
    "jobs",
    "media_objects",
    "outbound_actions",
    "request_states",
    "request_events",
    "messages",
    "idempotency_keys",
    "usage_ledger",
    "conversations",
    "tutor_assignments",
    "tutor_persona_versions",
    "entitlements",
    "audit_events",
    "tutors",
    "tutortwin_subjects",
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires a live PostgreSQL database")


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_DSN, pool_pre_ping=True)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Factory the entry service uses to open its own short transactions."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


_TRUNCATE_SQL = f"TRUNCATE {', '.join(sorted(_TABLES))} RESTART IDENTITY CASCADE"

TRUNCATE_ATTEMPTS = 3
"""A previous test's engine may still be releasing its connection when the next
truncation starts. That is a fixture race, not a product defect, so it is
retried rather than allowed to fail the run."""


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A clean session per test. Tables are truncated, never dropped."""
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        for attempt in range(1, TRUNCATE_ATTEMPTS + 1):
            try:
                # Bound the wait so a stuck lock surfaces as a clear timeout
                # rather than hanging the suite.
                await s.execute(text("SET LOCAL lock_timeout = '5s'"))
                await s.execute(text(_TRUNCATE_SQL))
                await s.commit()
                break
            except OperationalError:
                await s.rollback()
                if attempt == TRUNCATE_ATTEMPTS:
                    raise
                await asyncio.sleep(0.2 * attempt)
        try:
            yield s
        finally:
            await s.rollback()
