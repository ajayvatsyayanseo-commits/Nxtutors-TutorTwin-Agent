"""The fence that stops a migration dropping another product's tables.

TutorTwin's production database is **shared**. `public` holds tables owned by a
separate system; TutorTwin owns the `tutor_twin` schema and reaches it through a
`search_path` of `tutor_twin,public` - ours first so `CREATE` lands there, public
second so the `vector` extension type still resolves.

That second entry is the hazard. Reflection follows the search_path, so Alembic
autogenerate *sees* every foreign table, finds none of them in `target_metadata`,
and concludes they were deleted. `alembic check` merely reports it;
`alembic revision --autogenerate` writes `op.drop_table` for each one into a
migration file, and the next `upgrade head` executes it - destroying another
product's database from a command that looks entirely routine.

This module is the guard rail. It lives in the package rather than in
`migrations/env.py` because `env.py` executes Alembic setup at import time and
therefore cannot be unit-tested, and a safety control nobody can test is a
safety control nobody should trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Indexes created by raw SQL are invisible to `target_metadata`, so autogenerate
# proposes dropping them on every revision and `alembic check` never comes back
# clean. `ix_chunk_fulltext` is a functional GIN index over
# `to_tsvector('english', text)`, which SQLAlchemy's declarative layer cannot
# express; excluding it by name keeps the drift check meaningful instead of
# permanently red.
SQL_MANAGED_INDEXES: frozenset[str] = frozenset({"ix_chunk_fulltext"})

VERSION_TABLE = "alembic_version"


@dataclass(slots=True)
class MigrationScope:
    """What this migration run is allowed to see and change."""

    schema: str = "public"
    foreign_tables: set[str] = field(default_factory=set)
    """Every table the search_path exposes that is *not* ours. Empty when the
    database belongs to TutorTwin alone."""

    @property
    def is_shared(self) -> bool:
        return self.schema != "public"

    def include_object(
        self,
        obj: object,
        name: str | None,
        type_: str,
        reflected: bool,
        compare_to: object,
    ) -> bool:
        """Alembic's `include_object` hook.

        Returning False makes an object invisible to autogenerate - and we cannot
        drop what we cannot see.
        """
        if type_ == "table":
            # Another product's. Never ours to alter, never ours to drop.
            if reflected and name in self.foreign_tables:
                return False
            # Alembic's own bookkeeping. It is excluded automatically only while
            # `version_table_schema` is unset; naming a schema - which a shared
            # database requires, so our revision pointer never overwrites
            # another product's - brings it into view, where it looks like a
            # table nobody declared.
            if name == VERSION_TABLE:
                return False

        return not (type_ == "index" and name in SQL_MANAGED_INDEXES)


def load_foreign_tables(connection: object, schema: str) -> set[str]:
    """Every table on the search_path that belongs to somebody else.

    Queried rather than assumed: the set changes as the other product migrates,
    and a hard-coded list would go stale silently in the one direction that
    matters - a new foreign table appearing after ours was written.
    """
    if schema == "public":
        return set()

    from sqlalchemy import text

    rows = connection.execute(  # type: ignore[attr-defined]
        text("select tablename from pg_tables where schemaname <> :ours"),
        {"ours": schema},
    ).fetchall()
    return {row[0] for row in rows}


async def assert_no_cross_schema_fallthrough(session: object, schema: str) -> list[str]:
    """Every table we query must resolve to *our* schema, not somebody else's.

    The danger a shared `search_path` creates, in one sentence: if a table we
    declare is missing from our schema, an unqualified query silently falls
    through to `public` and reads another product's rows. Nothing errors. The
    data is simply wrong, and it is wrong in the direction of a privacy incident.

    `::regclass` is the authority here because it is *exactly* how Postgres
    resolves an unqualified name in a real query - search_path order included.
    Reading `pg_class` by name and taking the first row is not the same thing,
    and gets the answer wrong whenever the name exists twice.

    Returns the offending table names, empty when all is well.
    """
    if schema == "public":
        return []

    from sqlalchemy import text

    from tutortwin.db import (  # noqa: F401
        admin_models,
        knowledge_models,
        learning_models,
        subscription_models,
    )
    from tutortwin.db.models import Base

    offenders: list[str] = []
    for table in sorted(Base.metadata.tables):
        row = (
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "select n.nspname from pg_class c "
                    "join pg_namespace n on n.oid = c.relnamespace "
                    "where c.oid = to_regclass(:t)"
                ),
                {"t": table},
            )
        ).first()
        if row is None or row[0] != schema:
            offenders.append(f"{table} -> {row[0] if row else 'MISSING'}")
    return offenders


__all__ = [
    "SQL_MANAGED_INDEXES",
    "VERSION_TABLE",
    "MigrationScope",
    "assert_no_cross_schema_fallthrough",
    "load_foreign_tables",
]
