"""Helpers for the database tests: raw-SQL connections, seed rows and constraint expectations.

Seed helpers take keyword overrides so a test can start from a valid row and break exactly one
thing. Defaults that must be unique (codes, idempotency keys) are random, so tests that roll
back, and tests that commit, never collide with rows another test left behind.
"""

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import make_url

from app.db import CONNECT_TIMEOUT_SECONDS


def conninfo(url: str) -> str:
    """libpq connection string for a SQLAlchemy URL (psycopg cannot parse the +psycopg part)."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def maintenance_url(url: str) -> str:
    """The same server's maintenance database, which always exists."""
    return make_url(url).set(database="postgres").render_as_string(hide_password=False)


def connect(url: str, *, autocommit: bool = False, options: str = "") -> psycopg.Connection:
    """A new connection; ``options`` are extra libpq options such as ``-c lock_timeout=10s``."""
    return psycopg.connect(
        conninfo(url),
        autocommit=autocommit,
        options=f"-c timezone=UTC {options}".strip(),
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )


def current_revision(url: str) -> str | None:
    """The revision recorded in alembic_version, None when the table holds no row."""
    with connect(url, autocommit=True) as connection:
        row = connection.execute("SELECT max(version_num) FROM alembic_version").fetchone()
    assert row is not None
    return row[0]


@contextmanager
def violation(
    db: psycopg.Connection, error: type[psycopg.Error], constraint: str | None
) -> Iterator[None]:
    """Expect the block to fail with ``error`` raised by ``constraint``.

    The block runs in a savepoint, so the surrounding transaction is still usable afterwards.
    """
    with pytest.raises(error) as raised, db.transaction():
        yield
    assert raised.value.diag.constraint_name == constraint


def wait_until(
    condition: Callable[[], bool], *, timeout: float = 10.0, interval: float = 0.01
) -> bool:
    """Poll until ``condition()`` holds; False if it still does not after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _insert(db: psycopg.Connection, table: str, values: dict[str, Any]) -> int:
    columns = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    row = db.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) RETURNING id",
        list(values.values()),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_medicine(db: psycopg.Connection, **overrides: Any) -> int:
    return _insert(
        db,
        "medicines",
        {
            "code": _unique("MED"),
            "name": "Amoxil",
            "form": "capsule",
            "strength_value": 500,
            "strength_unit": "mg",
            "is_active": True,
        }
        | overrides,
    )


def insert_rule(
    db: psycopg.Connection,
    medicine_id: int,
    effective_from: date,
    effective_to: date | None = None,
    **overrides: Any,
) -> int:
    return _insert(
        db,
        "formulary_rules",
        {
            "medicine_id": medicine_id,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "max_quantity_per_dispense": 10,
            "max_quantity_per_30_days": 30,
            "requires_authorisation": False,
        }
        | overrides,
    )


def insert_dispense(
    db: psycopg.Connection, medicine_id: int, rule_id: int, **overrides: Any
) -> int:
    return _insert(
        db,
        "dispenses",
        {
            "medicine_id": medicine_id,
            "rule_id": rule_id,
            "patient_ref": "PT-1",
            "quantity": 1,
            "dispensed_at": datetime(2026, 3, 15, 10, tzinfo=UTC),
            "authorisation_ref": None,
            "idempotency_key": _unique("key"),
        }
        | overrides,
    )


def insert_attempt(db: psycopg.Connection, **overrides: Any) -> int:
    """Insert a dispense attempt; by default a valid rejection (it has a reason, no dispense)."""
    values = {
        "idempotency_key": _unique("key"),
        "request_hash": "0" * 64,
        "outcome": "rejected",
        "medicine_code": "MED-X",
        "medicine_id": None,
        "patient_ref": "PT-1",
        "quantity": 1,
        "dispensed_at": datetime(2026, 3, 15, 10, tzinfo=UTC),
        "authorisation_ref": None,
        "violations": [{"code": "medicine_not_found"}],
        "dispense_id": None,
    } | overrides
    values["violations"] = Jsonb(values["violations"])
    return _insert(db, "dispense_attempts", values)
