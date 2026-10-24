"""Database fixtures shared by every test package.

Nothing here connects to PostgreSQL at import time or at collection: a connection is made only
when a test requests one of the fixtures below, so tests/unit run with no database at all.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine, make_url, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import get_settings
from app.db import create_db_engine, create_session_factory

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
TEST_SUFFIX = "_test"


def make_alembic_config(url: str) -> Config:
    """An Alembic Config that migrates the database at ``url``."""
    config = Config(str(ALEMBIC_INI))
    # set_main_option goes through ConfigParser interpolation, where a literal % must be doubled
    # (URL-encoded passwords contain them).
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    # Keep pytest's logging set-up instead of the one alembic.ini describes.
    config.attributes["configure_logger"] = False
    return config


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


@contextmanager
def _maintenance_connection(url: URL) -> Iterator[Connection]:
    """A connection to the server's maintenance database, where CREATE/DROP DATABASE must run
    (outside a transaction block, hence AUTOCOMMIT)."""
    maintenance = create_db_engine(
        _render(url.set(database="postgres")),
        pool_size=1,
        max_overflow=0,
        statement_timeout_ms=60_000,
    ).execution_options(isolation_level="AUTOCOMMIT")
    try:
        with maintenance.connect() as connection:
            yield connection
    finally:
        maintenance.dispose()


def _drop_database(connection: Connection, name: str) -> None:
    quoted = connection.dialect.identifier_preparer.quote(name)
    connection.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))


def _create_database(connection: Connection, name: str) -> None:
    quoted = connection.dialect.identifier_preparer.quote(name)
    connection.execute(text(f"CREATE DATABASE {quoted}"))


@pytest.fixture(scope="session")
def database_url() -> str:
    """The database the tests may destroy: TEST_DATABASE_URL, else DATABASE_URL with a _test
    suffix on the database name."""
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = make_url(get_settings().database_url)
    # Always appended, even to a name that already ends in _test: the harness drops this
    # database, so it must never be one that DATABASE_URL points at.
    return _render(url.set(database=f"{url.database}{TEST_SUFFIX}"))


@pytest.fixture(scope="session")
def alembic_config() -> Callable[[str], Config]:
    """Factory: an Alembic Config that migrates the database at the given URL."""
    return make_alembic_config


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    """Engine on a freshly created database migrated with ``alembic upgrade head``, so every
    test run exercises the migrations."""
    url = make_url(database_url)
    if not (url.database or "").endswith(TEST_SUFFIX):
        pytest.exit(
            f"Refusing to drop and recreate database {url.database!r}: "
            f"a test database name must end in {TEST_SUFFIX!r}.",
            returncode=2,
        )
    with _maintenance_connection(url) as connection:
        _drop_database(connection, url.database)
        _create_database(connection, url.database)
    command.upgrade(make_alembic_config(database_url), "head")
    test_engine = create_db_engine(database_url)
    yield test_engine
    test_engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def clean_db(engine: Engine) -> None:
    """Opt-in: start the test with empty tables and identity counters back at 1."""
    with engine.begin() as connection:
        # A transaction leaked by an earlier test would otherwise make TRUNCATE wait forever.
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(
            text(
                "TRUNCATE medicines, formulary_rules, dispenses, dispense_attempts "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def scratch_database_factory(database_url: str) -> Iterator[Callable[[], str]]:
    """Creates empty, uniquely named databases (returning their URLs) and drops them after the
    test. For tests that must not share the migrated test database, such as migration tests."""
    base = make_url(database_url)
    created: list[URL] = []

    def create() -> str:
        scratch = base.set(database=f"{base.database}_{uuid.uuid4().hex[:8]}")
        with _maintenance_connection(scratch) as connection:
            _create_database(connection, scratch.database)
        created.append(scratch)
        return _render(scratch)

    yield create
    for scratch in created:
        with _maintenance_connection(scratch) as connection:
            _drop_database(connection, scratch.database)


@pytest.fixture
def scratch_database(scratch_database_factory: Callable[[], str]) -> str:
    return scratch_database_factory()
