"""Alembic environment.

Uses the *direct* DSN (`TUTORTWIN_DATABASE_MIGRATION_URL`), never the pooled one:
a transaction-pooling endpoint breaks DDL and advisory locks. Runs synchronously
with psycopg because migrations are a one-shot process where async buys nothing.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from tutortwin.config import get_settings
from tutortwin.db import (
    admin_models,  # noqa: F401  (registers tables)
    knowledge_models,  # noqa: F401  (registers tables)
    learning_models,  # noqa: F401  (registers tables)
    subscription_models,  # noqa: F401  (registers tables)
)
from tutortwin.db.migration_guard import MigrationScope, load_foreign_tables
from tutortwin.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _schema() -> str:
    return get_settings().database_postgres_schema


def _dsn() -> str:
    # Alembic drives a sync engine; strip the async driver marker.
    return get_settings().migration_dsn.replace("+psycopg_async", "+psycopg")


# The fence that keeps autogenerate away from another product's tables lives in
# `tutortwin.db.migration_guard`, because this file executes Alembic setup at
# import time and cannot be unit-tested - and a safety control nobody can test is
# a safety control nobody should trust.
_scope = MigrationScope(schema=_schema())
include_object = _scope.include_object


def run_migrations_offline() -> None:
    context.configure(
        url=_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
        version_table_schema=_schema(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _dsn()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    schema = _schema()

    with connectable.connect() as connection:
        if schema != "public":
            # A shared database. Three things have to be true before a single
            # CREATE TABLE runs, and all three are here rather than in a runbook
            # step somebody can forget:
            #
            #   1. the schema exists                    - created, never dropped
            #   2. unqualified DDL lands *inside* it    - search_path, ours first
            #   3. alembic's own version table is ours  - version_table_schema
            #
            # (3) is the one that bites: without it Alembic writes to
            # `public.alembic_version`, which on a shared database belongs to
            # somebody else's migrations and would be overwritten with our
            # revision id.
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            # Everything the search_path exposes that is not ours. Autogenerate
            # must not be able to propose dropping another product's tables.
            _scope.foreign_tables = load_foreign_tables(connection, schema)

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
            version_table_schema=schema,
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()

        # Commit explicitly.
        #
        # SQLAlchemy 2.0 connections are "commit as you go": `connect()` opens a
        # transaction that is ROLLED BACK when the block exits unless something
        # commits it. Alembic normally does that itself, but the explicit
        # `connection.commit()` above - needed so CREATE SCHEMA and the
        # search_path are visible to the migration that follows - ends the
        # transaction Alembic was tracking, and it does not open another one it
        # considers its own to commit.
        #
        # The failure mode is silent and expensive: `alembic upgrade head` logs
        # "Running upgrade ..." and exits 0, while the database keeps the old
        # revision and none of the new tables. Committing here is idempotent
        # when Alembic already did it.
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
