"""Migrations: every revision applies and reverts cleanly, and a full downgrade leaves nothing.

Everything runs against scratch databases created and dropped by the tests, never against the
migrated database that other tests share. "Exactly" means exactly: the schema snapshots taken
from the PostgreSQL catalogs (see catalog.py) must compare equal, not merely look similar.
"""

import io
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, pool, text

from alembic import command
from tests.db.catalog import SchemaSnapshot, schema_snapshot
from tests.db.support import current_revision

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
APPLICATION_TABLES = {"medicines", "formulary_rules", "dispenses", "dispense_attempts"}

ConfigFactory = Callable[[str], Config]


def revision_steps() -> list[tuple[str | None, str]]:
    """(previous revision, revision) for every migration, oldest first, read from the scripts so
    that a new migration is covered automatically."""
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    revisions = [revision.revision for revision in reversed(list(script.walk_revisions()))]
    return list(zip([None, *revisions[:-1]], revisions, strict=True))


@contextmanager
def connection_to(url: str) -> Iterator[Connection]:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def snapshot_of(url: str) -> SchemaSnapshot:
    with connection_to(url) as connection:
        return schema_snapshot(connection)


def tables_of(url: str) -> set[str]:
    with connection_to(url) as connection:
        rows = connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return set(rows.scalars())


def test_the_revisions_form_one_linear_history(alembic_config: ConfigFactory) -> None:
    script = ScriptDirectory.from_config(alembic_config("postgresql+psycopg://unused/unused"))
    assert len(script.get_heads()) == 1
    assert not [rev for rev in script.walk_revisions() if rev.is_branch_point or rev.is_merge_point]


def test_upgrade_head_creates_the_application_schema_and_stamps_the_head(
    scratch_database: str, alembic_config: ConfigFactory
) -> None:
    config = alembic_config(scratch_database)
    command.upgrade(config, "head")

    assert (
        current_revision(scratch_database) == ScriptDirectory.from_config(config).get_current_head()
    )
    assert tables_of(scratch_database) == APPLICATION_TABLES | {"alembic_version"}
    extensions = {name for (name,) in snapshot_of(scratch_database)["extensions"]}
    assert {"btree_gist", "pg_trgm"} <= extensions


def test_upgrade_head_is_repeatable_and_changes_nothing_the_second_time(
    scratch_database: str, alembic_config: ConfigFactory
) -> None:
    config = alembic_config(scratch_database)
    command.upgrade(config, "head")
    first = snapshot_of(scratch_database)
    command.upgrade(config, "head")
    assert snapshot_of(scratch_database) == first


def test_downgrade_to_base_leaves_only_the_version_table(
    scratch_database: str, alembic_config: ConfigFactory
) -> None:
    config = alembic_config(scratch_database)
    pristine = snapshot_of(scratch_database)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    after = snapshot_of(scratch_database)
    # Tables, sequences, indexes, constraints and extensions: all back to the empty database.
    assert after == pristine
    assert not {"btree_gist", "pg_trgm"} & {name for (name,) in after["extensions"]}
    assert tables_of(scratch_database) == {"alembic_version"}
    assert current_revision(scratch_database) is None


def test_head_base_head_reproduces_the_identical_schema(
    scratch_database: str, alembic_config: ConfigFactory
) -> None:
    config = alembic_config(scratch_database)
    command.upgrade(config, "head")
    first = snapshot_of(scratch_database)

    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert snapshot_of(scratch_database) == first


@pytest.mark.parametrize(
    ("previous", "revision"),
    revision_steps(),
    ids=[f"{previous or 'base'}-to-{revision}" for previous, revision in revision_steps()],
)
def test_each_revision_applies_reverts_and_applies_again(
    scratch_database: str, alembic_config: ConfigFactory, previous: str | None, revision: str
) -> None:
    config = alembic_config(scratch_database)
    if previous is not None:
        command.upgrade(config, previous)
    before = snapshot_of(scratch_database)

    command.upgrade(config, revision)
    applied = snapshot_of(scratch_database)
    assert current_revision(scratch_database) == revision
    assert applied != before

    command.downgrade(config, previous or "base")
    assert snapshot_of(scratch_database) == before
    assert current_revision(scratch_database) == previous

    command.upgrade(config, revision)
    assert snapshot_of(scratch_database) == applied
    assert current_revision(scratch_database) == revision


def test_offline_mode_renders_the_whole_history_as_sql_without_a_database(
    alembic_config: ConfigFactory,
) -> None:
    # The host does not exist: rendering must not need a connection, which also means no
    # migration may read from the database to decide what to do (0003 guards in SQL for that).
    config = alembic_config("postgresql+psycopg://nobody:secret@database.invalid/none")
    rendered = io.StringIO()
    config.output_buffer = rendered

    command.upgrade(config, "head", sql=True)

    sql = rendered.getvalue()
    steps = re.findall(r"^-- Running upgrade (\S*) -> (\S+)$", sql, flags=re.MULTILINE)
    assert steps == [(previous or "", revision) for previous, revision in revision_steps()]
    assert "CREATE EXTENSION IF NOT EXISTS btree_gist" in sql
    assert "RAISE EXCEPTION" in sql
