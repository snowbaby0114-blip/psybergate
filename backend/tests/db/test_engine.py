"""create_db_engine and create_session_factory: how every connection and session behaves."""

import time
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import Engine, exc, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config import get_settings
from app.db import CONNECT_TIMEOUT_SECONDS, create_db_engine
from app.models import Medicine
from tests.db.support import connect, maintenance_url, wait_until


@pytest.fixture
def small_engine(database_url: str) -> Iterator[Engine]:
    test_engine = create_db_engine(database_url, pool_size=1, max_overflow=0)
    yield test_engine
    test_engine.dispose()


def test_connections_use_utc_read_committed_and_the_configured_statement_timeout(
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("SHOW timezone")).scalar_one() == "UTC"
        assert connection.execute(text("SHOW transaction_isolation")).scalar_one() == (
            "read committed"
        )
        timeout_ms = connection.execute(
            text("SELECT setting::int FROM pg_settings WHERE name = 'statement_timeout'")
        ).scalar_one()
    assert timeout_ms == get_settings().statement_timeout_ms


def test_statement_timeout_cancels_slow_statements(database_url: str) -> None:
    limited = create_db_engine(database_url, statement_timeout_ms=200)
    try:
        started = time.monotonic()
        with pytest.raises(exc.OperationalError) as raised, limited.connect() as connection:
            connection.execute(text("SELECT pg_sleep(5)"))
        assert isinstance(raised.value.orig, psycopg.errors.QueryCanceled)
        assert time.monotonic() - started < 3
    finally:
        limited.dispose()


def test_connections_have_a_bounded_connect_timeout(small_engine: Engine) -> None:
    raw = small_engine.raw_connection()
    try:
        driver_connection = raw.driver_connection
        assert isinstance(driver_connection, psycopg.Connection)
        parameters = driver_connection.info.get_parameters()
    finally:
        raw.close()
    assert parameters["connect_timeout"] == str(CONNECT_TIMEOUT_SECONDS)


def test_pool_sizes_default_to_the_settings_and_can_be_overridden(
    database_url: str, small_engine: Engine
) -> None:
    default_engine = create_db_engine(database_url)
    try:
        default_pool = default_engine.pool
        assert isinstance(default_pool, QueuePool)
        assert default_pool.size() == get_settings().db_pool_size
    finally:
        default_engine.dispose()
    small_pool = small_engine.pool
    assert isinstance(small_pool, QueuePool)
    assert small_pool.size() == 1


def test_pool_pre_ping_replaces_a_connection_the_server_closed(
    small_engine: Engine, database_url: str
) -> None:
    with small_engine.connect() as connection:
        pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()

    with connect(maintenance_url(database_url), autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        gone = wait_until(
            lambda: (
                admin.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
                is None
            )
        )
    assert gone

    # Without pre-ping this checkout would hand out the dead connection and the query would fail.
    with small_engine.connect() as connection:
        assert connection.execute(text("SELECT pg_backend_pid()")).scalar_one() != pid


def test_sessions_keep_their_attributes_after_commit(
    session_factory: sessionmaker[Session], clean_db: None
) -> None:
    with session_factory() as session:
        medicine = Medicine(
            code="MED-1", name="Amoxil", form="capsule", strength_value=500, strength_unit="mg"
        )
        session.add(medicine)
        session.commit()
    # The session is closed, so the object is detached: with expire_on_commit=True reading any
    # attribute would raise DetachedInstanceError. The server-generated ones were fetched too.
    assert (medicine.code, medicine.id, medicine.is_active) == ("MED-1", 1, True)
    assert medicine.created_at.utcoffset() is not None


def test_sessions_do_not_flush_pending_objects_before_a_query(
    session_factory: sessionmaker[Session], clean_db: None
) -> None:
    count = select(func.count()).select_from(Medicine)
    with session_factory() as session:
        session.add(
            Medicine(
                code="MED-1", name="Amoxil", form="capsule", strength_value=500, strength_unit="mg"
            )
        )
        assert session.scalar(count) == 0
        session.flush()
        assert session.scalar(count) == 1
        session.rollback()
