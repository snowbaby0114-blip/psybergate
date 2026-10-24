"""When something goes wrong in the middle of POST /api/v1/dispenses: the request must leave no
trace of itself, and must be safe to send again with the same idempotency key.

The SPA does exactly that after a network error, a timeout or any 5xx, so a request that failed
may never have recorded half of its outcome.
"""

import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from typing import Any

import psycopg.errors
import pytest
from httpx import Response
from sqlalchemy import Engine, exc, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app import errors, repositories
from app.config import Settings
from app.locking import advisory_key
from app.models import FormularyRule, Medicine
from tests.api.dispense_support import (
    REPLAYED,
    Ledger,
    hold_advisory_lock,
    hold_medicine_row,
    post_dispense,
)
from tests.api.support import assert_problem

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]


@pytest.fixture
def ledger(session_factory: sessionmaker[Session]) -> Ledger:
    return Ledger(session_factory)


@pytest.fixture
def medicine(make_medicine: MedicineFactory) -> Medicine:
    return make_medicine(code="MED-0001")


@pytest.fixture
def rule(medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
    """At most 10 per dispense and 30 per 30 days, in force since 1 January 2026."""
    return make_rule(medicine, date(2026, 1, 1))


@pytest.fixture(autouse=True)
def _short_lock_timeout(settings: Settings) -> None:
    """A lock that a failed request leaked would otherwise make the retry wait for seconds."""
    settings.lock_timeout_ms = 500


class TestDatabaseErrorMapping:
    def test_a_deadlock_is_answered_like_a_lock_timeout_so_that_the_client_retries(self) -> None:
        deadlock = exc.OperationalError("SELECT 1", {}, psycopg.errors.DeadlockDetected("x"))

        problem = errors.problem_for_database_error(deadlock)

        assert problem is not None
        assert (problem.status, problem.code, problem.headers) == (
            503,
            "resource_busy",
            {"Retry-After": "1"},
        )

    def test_the_constraint_a_violation_is_about_is_named(self, engine: Engine) -> None:
        insert = text(
            "INSERT INTO medicines (code, name, form, strength_value, strength_unit) "
            "VALUES ('MED-DUP', 'n', 'f', 1, 'mg')"
        )

        with pytest.raises(exc.IntegrityError) as violation, engine.begin() as connection:
            connection.execute(insert)
            connection.execute(insert)

        assert errors.constraint_name(violation.value) == "uq_medicines_code"

    def test_an_error_that_does_not_come_from_the_driver_names_no_constraint(self) -> None:
        assert errors.constraint_name(exc.SQLAlchemyError("something else")) is None


def run_then_fail(monkeypatch: pytest.MonkeyPatch, name: str) -> Callable[..., Any]:
    """Make repositories.<name> do its work and then raise, so that its statement has already run
    inside the transaction when the request fails. Returns the real function."""
    real = getattr(repositories, name)

    def failing(*args: Any, **kwargs: Any) -> Any:
        real(*args, **kwargs)
        raise RuntimeError("the connection broke")

    monkeypatch.setattr(repositories, name, failing)
    return real


class TestAFailureLeavesNothingBehind:
    @pytest.mark.parametrize("failing", ["insert_dispense", "insert_attempt"])
    def test_an_accepted_request_that_fails_after_its_insert_records_neither_row_and_can_be_retried(
        self,
        error_client: TestClient,
        rule: FormularyRule,
        monkeypatch: pytest.MonkeyPatch,
        ledger: Ledger,
        failing: str,
    ) -> None:
        real = run_then_fail(monkeypatch, failing)

        failed = post_dispense(error_client)

        body = assert_problem(failed, 500, "internal_error")
        assert "connection broke" not in failed.text
        assert body["violations"] == []
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])
        monkeypatch.setattr(repositories, failing, real)

        retry = post_dispense(error_client)

        assert retry.status_code == 201
        assert REPLAYED not in retry.headers
        assert len(ledger.dispenses()) == 1
        assert len(ledger.attempts()) == 1

    def test_a_rejected_request_that_fails_after_logging_its_attempt_logs_nothing(
        self,
        error_client: TestClient,
        rule: FormularyRule,
        monkeypatch: pytest.MonkeyPatch,
        ledger: Ledger,
    ) -> None:
        real = run_then_fail(monkeypatch, "insert_attempt")

        failed = post_dispense(error_client, quantity=40)

        assert_problem(failed, 500, "internal_error")
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])
        monkeypatch.setattr(repositories, "insert_attempt", real)

        retry = post_dispense(error_client, quantity=40)

        assert retry.status_code == 422
        assert REPLAYED not in retry.headers
        assert len(ledger.attempts()) == 1

    def test_a_failure_before_anything_is_written_releases_its_locks(
        self,
        error_client: TestClient,
        rule: FormularyRule,
        monkeypatch: pytest.MonkeyPatch,
        ledger: Ledger,
    ) -> None:
        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the connection broke")

        real = repositories.pair_history
        monkeypatch.setattr(repositories, "pair_history", broken)

        failed = post_dispense(error_client)

        assert_problem(failed, 500, "internal_error")
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])
        monkeypatch.setattr(repositories, "pair_history", real)

        # The idempotency key and the patient's lock were taken before the failure. Had either
        # been kept, this would wait for the lock timeout and answer 503.
        assert post_dispense(error_client).status_code == 201

    def test_a_failure_does_not_count_against_the_quota(
        self,
        error_client: TestClient,
        rule: FormularyRule,
        monkeypatch: pytest.MonkeyPatch,
        ledger: Ledger,
    ) -> None:
        real = run_then_fail(monkeypatch, "insert_attempt")
        for number in range(3):
            post_dispense(error_client, idempotency_key=f"failed-{number}", quantity=10)
        monkeypatch.setattr(repositories, "insert_attempt", real)

        results = [
            post_dispense(error_client, idempotency_key=f"fits-{number}", quantity=10).status_code
            for number in range(3)
        ]

        assert results == [201, 201, 201]


class TestALockThatIsNotReleasedInTime:
    @pytest.fixture(params=["idempotency key", "medicine row", "patient and medicine"])
    def holding(
        self,
        request: pytest.FixtureRequest,
        session_factory: sessionmaker[Session],
        medicine: Medicine,
    ) -> AbstractContextManager[None]:
        if request.param == "idempotency key":
            return hold_advisory_lock(session_factory, "idem", "key-1")
        if request.param == "medicine row":
            return hold_medicine_row(session_factory, medicine.id, "UPDATE")
        return hold_advisory_lock(session_factory, "pair", medicine.id, "PT-1")

    def test_makes_the_request_a_503_that_leaves_nothing_and_can_be_retried_with_its_key(
        self,
        error_client: TestClient,
        rule: FormularyRule,
        holding: AbstractContextManager[None],
        ledger: Ledger,
    ) -> None:
        with holding:
            busy = post_dispense(error_client)

        assert_problem(busy, 503, "resource_busy")
        assert busy.headers["retry-after"] == "1"
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])

        retry = post_dispense(error_client)

        assert retry.status_code == 201
        assert REPLAYED not in retry.headers
        assert len(ledger.dispenses()) == 1
        assert len(ledger.attempts()) == 1


class TestADeadlock:
    """A deadlock needs a transaction that takes the locks in another order than ours, which no
    code of ours does. The test plays that other transaction: it holds the patient's lock and then
    asks for the medicine row FOR UPDATE, which the request holds FOR SHARE while it waits."""

    @staticmethod
    def someone_waits_for_an_advisory_lock(engine: Engine) -> bool:
        with engine.connect() as monitor:
            return bool(
                monitor.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                        "AND database = (SELECT oid FROM pg_database "
                        "WHERE datname = current_database())"
                    )
                ).scalar_one()
            )

    def test_is_a_503_that_names_the_deadlock_in_the_log_and_leaves_nothing_behind(
        self,
        error_client: TestClient,
        engine: Engine,
        session_factory: sessionmaker[Session],
        settings: Settings,
        medicine: Medicine,
        rule: FormularyRule,
        ledger: Ledger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Longer than the second PostgreSQL waits before it looks for a deadlock.
        settings.lock_timeout_ms = 10_000
        caplog.set_level(logging.WARNING, logger="app.errors")
        responses: list[Response] = []
        request = threading.Thread(target=lambda: responses.append(post_dispense(error_client)))

        with session_factory() as other, other.begin():
            # This side must not be the one PostgreSQL picks: it only checks after ten seconds.
            other.execute(text("SET LOCAL deadlock_timeout = '10s'"))
            other.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"),
                {"key": advisory_key("pair", medicine.id, "PT-1")},
            )
            request.start()
            waited = threading.Event()
            for _ in range(1000):
                if self.someone_waits_for_an_advisory_lock(engine):
                    break
                waited.wait(0.01)
            else:
                raise AssertionError("the request never waited for the patient's lock")
            # Now the request holds the medicine row FOR SHARE and waits for our lock: this
            # closes the cycle, and returns once PostgreSQL has aborted the request.
            other.execute(
                text("SELECT id FROM medicines WHERE id = :id FOR UPDATE"), {"id": medicine.id}
            )
        request.join(timeout=30)

        [deadlocked] = responses
        assert_problem(deadlocked, 503, "resource_busy")
        assert deadlocked.headers["retry-after"] == "1"
        assert "sqlstate 40P01" in caplog.text
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])

        retry = post_dispense(error_client)

        assert retry.status_code == 201
        assert REPLAYED not in retry.headers
