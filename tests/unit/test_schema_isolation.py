"""The fence around another product's tables.

TutorTwin's production database is **shared**: `public` holds 61 tables owned by
a separate memory/agent system, including its own `alembic_version` and a
`prompt_versions` table whose name TutorTwin also uses.

TutorTwin therefore lives in its own schema, reached through a `search_path` of
`tutor_twin,public` - ours first so `CREATE` lands there, `public` second so the
`vector` extension type still resolves.

That second entry is what makes these tests necessary. Reflection follows the
search_path, so Alembic autogenerate *sees* all 61 foreign tables, finds none of
them in `target_metadata`, and concludes they were deleted. `alembic check` only
reports that. `alembic revision --autogenerate` would write `op.drop_table` for
each one into a migration file, and the next `upgrade head` would execute it -
destroying another product's database from a command that looks routine.

These tests are the guard rail. If one fails, do not "fix the test".
"""

from __future__ import annotations

import pytest

from tutortwin.config import Settings


class TestSearchPath:
    def test_our_schema_comes_first_so_create_lands_in_it(self) -> None:
        settings = Settings(environment="test", database_postgres_schema="tutor_twin")
        assert settings.search_path.split(",")[0] == "tutor_twin"

    def test_public_is_still_reachable_for_extension_types(self) -> None:
        """`vector` is installed in public. Drop it from the path and every RAG
        table fails to create with 'type vector does not exist'."""
        settings = Settings(environment="test", database_postgres_schema="tutor_twin")
        assert "public" in settings.search_path.split(",")

    def test_no_space_after_the_comma(self) -> None:
        """It travels in libpq's `options`, where a space separates arguments."""
        settings = Settings(environment="test", database_postgres_schema="tutor_twin")
        assert " " not in settings.search_path

    def test_a_sole_owner_needs_no_second_entry(self) -> None:
        settings = Settings(environment="test", database_postgres_schema="public")
        assert settings.search_path == "public"

    @pytest.mark.parametrize(
        "bad",
        [
            'x";drop schema public cascade;--',
            "public, tutor_twin",
            "Tutor_Twin",
            "1twin",
            "",
        ],
    )
    def test_a_schema_name_that_is_not_an_identifier_is_refused(self, bad: str) -> None:
        """It reaches SQL as an identifier, never as a bound parameter."""
        with pytest.raises(ValueError, match="lowercase identifier"):
            Settings(environment="test", database_postgres_schema=bad)


class TestAutogenerateFence:
    """`include_object` must be blind to anything outside our schema."""

    def _env(self, foreign: set[str]):
        from tutortwin.db.migration_guard import MigrationScope

        return MigrationScope(schema="tutor_twin", foreign_tables=foreign)

    def test_a_foreign_table_is_invisible_to_autogenerate(self) -> None:
        """The one that matters: their table must not be droppable.

        `reflected=True` and not in our metadata is exactly the shape
        autogenerate turns into `op.drop_table`.
        """
        env = self._env({"memory_facts", "agent_registry", "glue_outbox"})

        for name in ("memory_facts", "agent_registry", "glue_outbox"):
            assert not env.include_object(None, name, "table", True, None), (
                f"{name} is another product's table and must never be visible to autogenerate"
            )

    def test_our_own_tables_are_still_visible(self) -> None:
        """The fence must not blind us to our own drift, or it is useless."""
        env = self._env({"memory_facts"})

        assert env.include_object(None, "tutortwin_subjects", "table", True, None)
        assert env.include_object(None, "conversations", "table", True, None)

    def test_the_version_table_is_excluded(self) -> None:
        """Naming a version_table_schema brings Alembic's own bookkeeping into
        view, where it looks like a table nobody declared."""
        env = self._env(set())
        assert not env.include_object(None, "alembic_version", "table", True, None)

    def test_an_unshared_database_needs_no_fence(self) -> None:
        """With no foreign tables recorded, nothing is hidden."""
        env = self._env(set())
        assert env.include_object(None, "anything_at_all", "table", True, None)

    def test_the_sql_managed_index_exclusion_survives(self) -> None:
        """A pre-existing exclusion the fence must not have broken."""
        env = self._env(set())
        assert not env.include_object(None, "ix_chunk_fulltext", "index", True, None)


class TestNamesThatCollide:
    def test_prompt_versions_exists_in_both_products(self) -> None:
        """The concrete reason a schema was necessary rather than merely tidy.

        Creating TutorTwin's `prompt_versions` in `public` would have collided
        with a table the other product already owns.
        """
        from tutortwin.db import admin_models, knowledge_models, learning_models  # noqa: F401
        from tutortwin.db.models import Base

        assert "prompt_versions" in Base.metadata.tables, (
            "TutorTwin still defines prompt_versions; if this ever stops being "
            "true the collision note above needs revisiting, but the fence stays"
        )
