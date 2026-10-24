"""Fixtures for the API tests: the real application, on the real migrated test database.

`clean_db` (from tests/conftest.py) empties every table before every test here. Factories insert
through their own short-lived sessions and commit, so the application, which uses other
connections, sees the rows exactly as it would in production.
"""

import itertools
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import StarletteDeprecationWarning

from app.config import Settings
from app.main import create_app
from app.models import Dispense, FormularyRule, Medicine
from tests.api.support import FakeClock

with warnings.catch_warnings():
    # Starlette's test client now prefers httpx2 and warns when it finds the httpx this project
    # pins; the warning is about the dependency, not about anything the tests do.
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def _empty_tables(clean_db: None) -> None:
    return None


@pytest.fixture
def clock() -> FakeClock:
    # 12:00 in Johannesburg on Monday 15 June 2026.
    return FakeClock(datetime(2026, 6, 15, 10, 0, tzinfo=UTC))


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(database_url=database_url)


@pytest.fixture
def app(engine: Engine, settings: Settings, clock: FakeClock) -> FastAPI:
    return create_app(engine=engine, settings=settings, clock=clock)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Re-raises unhandled application exceptions, so a bug shows as its own traceback."""
    return TestClient(app)


@pytest.fixture
def error_client(app: FastAPI) -> TestClient:
    """Answers an unhandled exception with the 500 response a real client would receive."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def make_medicine(session_factory: sessionmaker[Session]) -> Callable[..., Medicine]:
    """Insert a medicine. Codes count up from MED-0001 within a test; override any column."""
    numbers = itertools.count(1)

    def make(**overrides: Any) -> Medicine:
        values: dict[str, Any] = {
            "code": f"MED-{next(numbers):04d}",
            "name": "Amoxil",
            "form": "capsule",
            "strength_value": Decimal(500),
            "strength_unit": "mg",
            "is_active": True,
        } | overrides
        medicine = Medicine(**values)
        with session_factory() as session, session.begin():
            session.add(medicine)
        return medicine

    return make


@pytest.fixture
def make_rule(session_factory: sessionmaker[Session]) -> Callable[..., FormularyRule]:
    """Insert a rule period [effective_from, effective_to) for a medicine; override any column."""

    def make(
        medicine: Medicine,
        effective_from: date,
        effective_to: date | None = None,
        **overrides: Any,
    ) -> FormularyRule:
        values: dict[str, Any] = {
            "medicine_id": medicine.id,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "max_quantity_per_dispense": 10,
            "max_quantity_per_30_days": 30,
            "requires_authorisation": False,
        } | overrides
        rule = FormularyRule(**values)
        with session_factory() as session, session.begin():
            session.add(rule)
        return rule

    return make


@pytest.fixture
def make_dispense(session_factory: sessionmaker[Session]) -> Callable[..., Dispense]:
    """Insert a dispense directly (no validation, no attempt row); the caller names the rule that
    governs it. Idempotency keys count up within a test; override any column."""
    numbers = itertools.count(1)

    def make(medicine: Medicine, rule: FormularyRule, **overrides: Any) -> Dispense:
        values: dict[str, Any] = {
            "medicine_id": medicine.id,
            "rule_id": rule.id,
            "patient_ref": "PT-1",
            "quantity": 1,
            "dispensed_at": datetime(2026, 3, 15, 10, 0, tzinfo=UTC),
            "authorisation_ref": None,
            "idempotency_key": f"key-{next(numbers):04d}",
        } | overrides
        dispense = Dispense(**values)
        with session_factory() as session, session.begin():
            session.add(dispense)
        return dispense

    return make


@pytest.fixture
def read_rules(session_factory: sessionmaker[Session]) -> Callable[[Medicine], list[FormularyRule]]:
    """The medicine's rules as stored right now, ascending by effective_from."""

    def read(medicine: Medicine) -> list[FormularyRule]:
        stmt = (
            select(FormularyRule)
            .where(FormularyRule.medicine_id == medicine.id)
            .order_by(FormularyRule.effective_from)
        )
        with session_factory() as session:
            return list(session.scalars(stmt))

    return read


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A session for queries the test makes itself; closed after the test."""
    with session_factory() as session:
        yield session
