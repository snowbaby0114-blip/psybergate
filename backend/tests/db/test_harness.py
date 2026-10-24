"""The shared fixtures themselves: migrated engine, clean_db, scratch databases, Alembic config."""

from collections.abc import Callable, Iterator
from datetime import date

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, make_url, text

from tests.db.support import (
    connect,
    insert_attempt,
    insert_dispense,
    insert_medicine,
    insert_rule,
    maintenance_url,
)

TABLES = ("medicines", "formulary_rules", "dispenses", "dispense_attempts")


@pytest.fixture
def after_teardown() -> Iterator[list[Callable[[], None]]]:
    """Checks to run once the fixtures requested after this one have been torn down (fixtures
    are torn down in reverse order of setup)."""
    checks: list[Callable[[], None]] = []
    yield checks
    for check in checks:
        check()


def test_engine_fixture_points_at_a_test_database_migrated_to_head(
    engine: Engine, alembic_config: Callable[[str], Config], database_url: str
) -> None:
    head = ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
        assert revision == head
        assert set(tables) >= set(TABLES)
    assert engine.url.database == make_url(database_url).database
    assert engine.url.database is not None
    assert engine.url.database.endswith("_test")


@pytest.fixture
def committed_rows(engine: Engine, database_url: str) -> None:
    """One committed row in every table, as an earlier test might leave behind."""
    with connect(database_url) as connection:
        medicine = insert_medicine(connection)
        rule = insert_rule(connection, medicine, date(2026, 1, 1))
        dispense = insert_dispense(connection, medicine, rule)
        insert_attempt(
            connection,
            outcome="accepted",
            dispense_id=dispense,
            violations=[],
            medicine_id=medicine,
        )


def test_clean_db_empties_every_table_and_restarts_identities(
    committed_rows: None, clean_db: None, database_url: str
) -> None:
    # Fixtures are set up in argument order: the rows exist before the cleanup under test runs.
    with connect(database_url) as connection:
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()
            for table in TABLES
        }
        assert counts == dict.fromkeys(TABLES, (0,))
        assert insert_medicine(connection) == 1
        connection.rollback()


def test_db_fixture_leaves_nothing_behind(
    after_teardown: list[Callable[[], None]], db: psycopg.Connection, database_url: str
) -> None:
    code = "ROLLBACK-PROBE"
    insert_medicine(db, code=code)
    assert db.execute("SELECT count(*) FROM medicines WHERE code = %s", (code,)).fetchone() == (1,)

    def nothing_left() -> None:
        with connect(database_url) as other:
            count = other.execute("SELECT count(*) FROM medicines WHERE code = %s", (code,))
            assert count.fetchone() == (0,)

    after_teardown.append(nothing_left)


def test_scratch_database_is_empty_and_separate_from_the_test_database(
    scratch_database: str, database_url: str
) -> None:
    assert make_url(scratch_database).database != make_url(database_url).database
    with connect(scratch_database) as connection:
        tables = connection.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
        ).fetchone()
    assert tables == (0,)


def test_scratch_database_is_dropped_after_the_test(
    after_teardown: list[Callable[[], None]], scratch_database: str, database_url: str
) -> None:
    name = make_url(scratch_database).database

    def dropped() -> None:
        with connect(maintenance_url(database_url), autocommit=True) as connection:
            found = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            assert found.fetchone() is None

    after_teardown.append(dropped)
    with connect(scratch_database) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


def test_alembic_config_keeps_percent_encoded_urls_intact(
    alembic_config: Callable[[str], Config],
) -> None:
    url = "postgresql+psycopg://user:p%40ss@localhost/db"
    # ConfigParser interpolation would reject the bare % if the helper did not escape it.
    assert alembic_config(url).get_main_option("sqlalchemy.url") == url
