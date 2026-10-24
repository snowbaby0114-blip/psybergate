from collections.abc import Iterator
from datetime import date

import psycopg
import pytest

from tests.db.support import connect, insert_medicine, insert_rule


@pytest.fixture
def db(engine: object, database_url: str) -> Iterator[psycopg.Connection]:
    """A connection to the migrated test database, inside a transaction that is always rolled
    back: a test can insert freely and leave nothing behind. (``engine`` makes sure the
    database exists and is migrated.)"""
    with connect(database_url) as connection, connection.transaction(force_rollback=True):
        yield connection


@pytest.fixture
def governed(db: psycopg.Connection) -> tuple[int, int]:
    """A medicine and an open-ended rule on it from 2025-01-01: the parents a dispense needs."""
    medicine = insert_medicine(db)
    return medicine, insert_rule(db, medicine, date(2025, 1, 1))
