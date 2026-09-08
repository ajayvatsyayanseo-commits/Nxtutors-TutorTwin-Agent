"""Migrations must build the schema from empty, and reverse cleanly.

This runs against a throwaway database so it never disturbs the test data other
integration tests rely on.
"""

from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tutortwin.config import get_settings

from ..conftest import TEST_DSN

pytestmark = pytest.mark.integration

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXPECTED_TABLES = {
    "admin_login_attempts",
    "admin_sessions",
    "admin_users",
    "assessment_attempts",
    "assessment_questions",
    "assessments",
    "attempt_responses",
    "audit_events",
    # Phase 09: subscriptions and payments.
    "payments",
    "signups",
    "conversation_summaries",
    "conversations",
    "document_chunks",
    "embedding_cache",
    "entitlements",
    "feature_flags",
    "flashcard_decks",
    "flashcard_reviews",
    "flashcards",
    "homework_tasks",
    "idempotency_keys",
    "jobs",
    "knowledge_sources",
    "learning_artifacts",
    "media_extractions",
    "media_objects",
    "messages",
    "model_catalog",
    "outbound_actions",
    "plan_policies",
    "prompt_versions",
    "request_events",
    "request_states",
    "retrieval_events",
    "student_memories",
    "study_notes",
    "topic_stats",
    "tutor_assignments",
    "tutor_persona_versions",
    "tutors",
    "tutortwin_subjects",
    "usage_ledger",
}


def _sync_dsn(dsn: str) -> str:
    """Sync DSN pinned to psycopg 3; a bare postgresql:// would select psycopg2."""
    if dsn.startswith("postgresql+psycopg://"):
        return dsn
    return dsn.replace("postgresql://", "postgresql+psycopg://", 1)


def _alembic_config(dsn: str) -> Config:
    config = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(REPO_ROOT, "migrations"))
    os.environ["TUTORTWIN_DATABASE_MIGRATION_URL"] = dsn
    # Settings is lru_cached; without this the second call in a session would
    # reuse the previous test's (now dropped) scratch DSN.
    get_settings.cache_clear()
    return config


@pytest.fixture
def scratch_database() -> str:
    """A fresh empty database, dropped afterwards."""
    name = f"tutortwin_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(_sync_dsn(TEST_DSN), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()

    dsn = TEST_DSN.rsplit("/", 1)[0] + "/" + name
    previous = os.environ.get("TUTORTWIN_DATABASE_MIGRATION_URL")
    try:
        yield dsn
    finally:
        if previous is None:
            os.environ.pop("TUTORTWIN_DATABASE_MIGRATION_URL", None)
        else:
            os.environ["TUTORTWIN_DATABASE_MIGRATION_URL"] = previous
        admin = create_engine(_sync_dsn(TEST_DSN), isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _tables(dsn: str) -> set[str]:
    engine = create_engine(_sync_dsn(dsn))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            )
            return {row[0] for row in rows}
    finally:
        engine.dispose()


def test_migrations_apply_to_empty_database(scratch_database: str) -> None:
    assert _tables(scratch_database) == set()

    config = _alembic_config(scratch_database)
    command.upgrade(config, "head")

    assert _tables(scratch_database) == EXPECTED_TABLES


def test_migrations_downgrade_and_reapply(scratch_database: str) -> None:
    config = _alembic_config(scratch_database)

    command.upgrade(config, "head")
    assert _tables(scratch_database) == EXPECTED_TABLES

    command.downgrade(config, "base")
    assert _tables(scratch_database) == set()

    command.upgrade(config, "head")
    assert _tables(scratch_database) == EXPECTED_TABLES


def test_idempotency_unique_constraint_exists(scratch_database: str) -> None:
    """The dedupe guarantee is a database constraint, not application hope."""
    config = _alembic_config(scratch_database)
    command.upgrade(config, "head")

    engine = create_engine(_sync_dsn(scratch_database))
    try:
        with engine.connect() as conn:
            found = conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conname = 'uq_idempotency_key' AND contype = 'u'"
                )
            ).scalar_one_or_none()
            assert found == "uq_idempotency_key"

            conn.execute(
                text(
                    "INSERT INTO idempotency_keys (id, key, response_json) "
                    "VALUES (gen_random_uuid(), 'dupe', '{}'::jsonb)"
                )
            )
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO idempotency_keys (id, key, response_json) "
                        "VALUES (gen_random_uuid(), 'dupe', '{}'::jsonb)"
                    )
                )
    finally:
        engine.dispose()
