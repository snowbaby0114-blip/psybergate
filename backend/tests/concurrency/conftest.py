"""Fixtures of the concurrency tests: the real application on the real migrated test database.

Threads race through separate requests, so each one gets a connection of its own from the pool.
The pool is sized for the largest race, and `clean_db` empties every table before every test.
"""

import socket
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import StarletteDeprecationWarning

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.main import create_app
from app.models import FormularyRule, Medicine
from tests.api.dispense_support import Ledger
from tests.api.support import FakeClock
from tests.concurrency.support import (
    WAIT_SECONDS,
    DatabaseErrors,
    EventLog,
    Gate,
    LiveServer,
    MedicineFactory,
    RuleFactory,
    finish_requests_in_flight,
)

with warnings.catch_warnings():
    # Starlette's test client now prefers httpx2 and warns when it finds the httpx this project
    # pins; the warning is about the dependency, not about anything the tests do.
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

# More than the largest race needs (24 requests, plus the monitor and the test itself).
POOL_SIZE = 32
# A request that a bug leaves waiting must fail the test, not make it hang; the lock timeout tests
# choose their own, short one.
LOCK_TIMEOUT_MS = 20_000
STATEMENT_TIMEOUT_MS = 60_000


@pytest.fixture(autouse=True)
def _empty_tables(clean_db: None) -> None:
    return None


@pytest.fixture(scope="session")
def racing_engine(engine: Engine, database_url: str) -> Iterator[Engine]:
    """An engine on the migrated test database with room for every thread of a race. `engine`
    is only needed to have the database created and migrated."""
    racing = create_db_engine(
        database_url,
        pool_size=POOL_SIZE,
        max_overflow=8,
        statement_timeout_ms=STATEMENT_TIMEOUT_MS,
    )
    yield racing
    racing.dispose()


@pytest.fixture(scope="session")
def racing_sessions(racing_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(racing_engine)


@pytest.fixture
def clock() -> FakeClock:
    # 12:00 in Johannesburg on Monday 15 June 2026: after every date the tests dispense on.
    return FakeClock(datetime(2026, 6, 15, 10, 0, tzinfo=UTC))


@pytest.fixture
def build_app(racing_engine: Engine, database_url: str, clock: FakeClock) -> Callable[..., FastAPI]:
    def build(**settings: Any) -> FastAPI:
        defaults: dict[str, Any] = {
            "database_url": database_url,
            "lock_timeout_ms": LOCK_TIMEOUT_MS,
            "statement_timeout_ms": STATEMENT_TIMEOUT_MS,
        }
        return create_app(
            engine=racing_engine, settings=Settings(**(defaults | settings)), clock=clock
        )

    return build


@pytest.fixture
def app(build_app: Callable[..., FastAPI]) -> FastAPI:
    return build_app()


@pytest.fixture
def new_client(app: FastAPI) -> Iterator[Callable[[], TestClient]]:
    """Every call makes a client of its own: a thread never shares one, as it never shares a
    session. They answer an unhandled exception with the 500 a real client would receive."""
    clients: list[TestClient] = []

    def make() -> TestClient:
        clients.append(TestClient(app, raise_server_exceptions=False))
        return clients[-1]

    yield make
    for client in clients:
        client.close()


@pytest.fixture
def live_server(app: FastAPI) -> Iterator[LiveServer]:
    """A real HTTP server in a thread of this process: requests cross a socket and are served by
    uvicorn's event loop and thread pool, as in production."""
    # Bound here, port 0 asks the system for a free one, and handed to uvicorn already listening:
    # nothing can take the port in between.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + WAIT_SECONDS
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the server did not start")
        time.sleep(0.01)
    live = LiveServer(f"http://127.0.0.1:{listener.getsockname()[1]}")
    yield live
    live.close_clients()
    server.should_exit = True
    thread.join(WAIT_SECONDS)
    listener.close()


@pytest.fixture
def ledger(racing_sessions: sessionmaker[Session]) -> Ledger:
    return Ledger(racing_sessions)


@pytest.fixture
def db_errors(racing_engine: Engine) -> Iterator[DatabaseErrors]:
    errors = DatabaseErrors(racing_engine)
    yield errors
    errors.stop()


@pytest.fixture
def log() -> EventLog:
    return EventLog()


@pytest.fixture
def hold(monkeypatch: pytest.MonkeyPatch, log: EventLog) -> Iterator[Callable[..., Gate]]:
    """Install a Gate on a function of the service. Whatever a test leaves held is let go when it
    ends, and its requests are waited for, so that a failed test cannot leave a transaction open
    for the next one."""
    gates: list[Gate] = []

    def install(module: Any, name: str, **options: Any) -> Gate:
        gate = Gate(log, **options)
        gate.install(monkeypatch, module, name)
        gates.append(gate)
        return gate

    yield install
    for gate in gates:
        gate.release()
    finish_requests_in_flight()


@pytest.fixture
def make_medicine(racing_sessions: sessionmaker[Session]) -> MedicineFactory:
    """Insert a medicine. Codes count up from MED-0001 within a test."""
    created: list[Medicine] = []

    def make(**overrides: Any) -> Medicine:
        values: dict[str, Any] = {
            "code": f"MED-{len(created) + 1:04d}",
            "name": "Amoxil",
            "form": "capsule",
            "strength_value": Decimal(500),
            "strength_unit": "mg",
            "is_active": True,
        } | overrides
        medicine = Medicine(**values)
        with racing_sessions() as session, session.begin():
            session.add(medicine)
        created.append(medicine)
        return medicine

    return make


@pytest.fixture
def make_rule(racing_sessions: sessionmaker[Session]) -> RuleFactory:
    """Insert a rule period [effective_from, effective_to) of 100 per dispense and 100 per 30
    days, unless told otherwise."""

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
            "max_quantity_per_dispense": 100,
            "max_quantity_per_30_days": 100,
            "requires_authorisation": False,
        } | overrides
        rule = FormularyRule(**values)
        with racing_sessions() as session, session.begin():
            session.add(rule)
        return rule

    return make


@pytest.fixture
def medicine(make_medicine: MedicineFactory) -> Medicine:
    return make_medicine()


@pytest.fixture
def rule(medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
    """At most 100 per dispense and 100 per 30 days, in force since 1 January 2026."""
    return make_rule(medicine, date(2026, 1, 1))
