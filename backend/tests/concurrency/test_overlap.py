"""Rule 9, demonstrated: two requests for one patient and one medicine that arrive together cannot
jointly breach max_quantity_per_30_days.

The choreography, in one minute. The cap is 100 per 30 days and each request asks for 60.

1. Request A is held inside its transaction, after it has read the patient's history (nothing yet)
   and while it holds all of its locks.
2. Only then is request B started. A monitor connection waits until it has seen B blocked on the
   patient-and-medicine advisory lock, and the holder of that lock is A's backend.
3. Only then is A released. It commits; B gets the lock, reads a history that now holds A's 60 and
   is rejected.

Nothing depends on timing: B is released from its wait by A's commit and by nothing else, and the
test lets A go only once it has seen B waiting. The two controls below run the same choreography
against a service with the protection taken away, and assert that the cap is then breached: that
is what shows that this harness can detect the race, and that the lock, and reading under it, are
what prevent it.
"""

import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import Engine
from starlette.testclient import TestClient

from app import locking, repositories
from app.domain.dispense import PriorDispense
from app.locking import advisory_key
from app.models import FormularyRule, Medicine
from tests.api.dispense_support import Ledger
from tests.api.support import assert_problem
from tests.concurrency.monitor import (
    advisory_lock_holders,
    wait_for_waiters,
    waiting_for_advisory_lock,
)
from tests.concurrency.support import DatabaseErrors, EventLog, Gate, answer, send, start

pytestmark = pytest.mark.concurrency

CAP = 100
REQUESTED = 60
PATIENT = "PT-1"


def read_history(history: list[PriorDispense]) -> str:
    return f"read a history of {[prior.quantity for prior in history]}"


@pytest.fixture
def clients(new_client: Callable[[], TestClient]) -> tuple[TestClient, TestClient]:
    return new_client(), new_client()


@pytest.fixture
def held(hold: Callable[..., Gate]) -> Gate:
    """Holds the first request after it has read its history, the way the service does it."""
    return hold(repositories, "pair_history", describe=read_history)


def start_a(clients: tuple[TestClient, TestClient]) -> Future[Response]:
    return start(lambda: send(clients[0], idempotency_key="key-A", quantity=REQUESTED))


def start_b(clients: tuple[TestClient, TestClient]) -> Future[Response]:
    return start(lambda: send(clients[1], idempotency_key="key-B", quantity=REQUESTED))


def recorded_quantities(ledger: Ledger) -> list[int]:
    return [dispense.quantity for dispense in ledger.dispenses()]


def test_the_second_request_waits_for_the_first_and_is_rejected(
    clients: tuple[TestClient, TestClient],
    held: Gate,
    log: EventLog,
    racing_engine: Engine,
    medicine: Medicine,
    rule: FormularyRule,
    ledger: Ledger,
    db_errors: DatabaseErrors,
) -> None:
    pair_lock = advisory_key("pair", medicine.id, PATIENT)
    first = start_a(clients)
    held.wait_until_inside()

    log.record("B starts")
    second = start_b(clients)
    [waiter] = wait_for_waiters(
        racing_engine, "waiting for the patient's lock", waiting_for_advisory_lock(pair_lock)
    )
    observed_blocked_waiter = set(waiter.blocked_by) == advisory_lock_holders(
        racing_engine, pair_lock
    )
    log.record("B is seen waiting")

    log.record("A is released")
    held.release()
    accepted, rejected = answer(first), answer(second)

    assert observed_blocked_waiter is True
    assert accepted.status_code == 201
    problem = assert_problem(
        rejected, 422, "dispense_rejected", extra_keys=frozenset({"attempt_id"})
    )
    [violation] = problem["violations"]
    assert (violation["code"], violation["field"]) == (
        "max_quantity_per_30_days_exceeded",
        "quantity",
    )
    assert violation["context"] == {
        "limit": CAP,
        "already_dispensed": REQUESTED,
        "requested": REQUESTED,
        "window_start": "2026-02-14",
        "window_end": "2026-03-15",
        "rule_id": rule.id,
    }
    assert recorded_quantities(ledger) == [REQUESTED]
    accepted_attempt, rejected_attempt = ledger.attempts()
    assert (accepted_attempt.idempotency_key, accepted_attempt.outcome) == ("key-A", "accepted")
    assert (rejected_attempt.idempotency_key, rejected_attempt.outcome) == ("key-B", "rejected")
    assert rejected_attempt.id == problem["attempt_id"]
    assert [v["code"] for v in rejected_attempt.violations] == ["max_quantity_per_30_days_exceeded"]
    assert rejected_attempt.dispense_id is None
    # B read its history only after A had committed: what it read contains A's dispense.
    assert log.events == [
        "A read a history of []",
        "B starts",
        "B is seen waiting",
        "A is released",
        "B read a history of [60]",
    ]
    assert db_errors.seen == []


def test_control_without_the_patient_lock_both_are_admitted_and_the_cap_is_breached(
    clients: tuple[TestClient, TestClient],
    held: Gate,
    log: EventLog,
    monkeypatch: pytest.MonkeyPatch,
    rule: FormularyRule,
    ledger: Ledger,
) -> None:
    """The same choreography with locking.lock_patient_medicine taking no lock.

    B is never held up: it runs to completion while A is still held, having read a history of
    nothing, and both are admitted. This asserts the breach. It shows that the harness detects the
    race, and that the patient-and-medicine lock is what prevents it in the test above.
    """
    monkeypatch.setattr(locking, "lock_patient_medicine", lambda *args, **kwargs: None)
    first = start_a(clients)
    held.wait_until_inside()

    log.record("B starts")
    second = start_b(clients)
    answered_while_a_was_held = answer(second)
    log.record("B is answered")

    log.record("A is released")
    held.release()
    answered_after_being_held = answer(first)

    assert answered_while_a_was_held.status_code == answered_after_being_held.status_code == 201
    assert sum(recorded_quantities(ledger)) == 2 * REQUESTED
    assert sum(recorded_quantities(ledger)) > CAP
    assert log.events == [
        "A read a history of []",
        "B starts",
        "B read a history of []",
        "B is answered",
        "A is released",
    ]


@pytest.fixture
def quota_read_before_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service with the two steps swapped: the lock is still taken and kept until the commit,
    but only after the history has been read."""
    take_the_lock = locking.lock_patient_medicine
    read_the_history = repositories.pair_history
    asked_for = threading.local()

    def lock_after_the_read(session: Any, medicine_id: int, patient_ref: str) -> None:
        asked_for.lock = (session, medicine_id, patient_ref)

    def read_then_lock(session: Any, **kwargs: Any) -> list[PriorDispense]:
        history = read_the_history(session, **kwargs)
        take_the_lock(*asked_for.lock)
        return history

    monkeypatch.setattr(locking, "lock_patient_medicine", lock_after_the_read)
    monkeypatch.setattr(repositories, "pair_history", read_then_lock)


@pytest.mark.usefixtures("quota_read_before_the_lock")
def test_control_reading_the_quota_before_the_lock_makes_the_second_wait_in_vain(
    clients: tuple[TestClient, TestClient],
    held: Gate,
    log: EventLog,
    racing_engine: Engine,
    medicine: Medicine,
    rule: FormularyRule,
    ledger: Ledger,
) -> None:
    """The same choreography with the history read before the lock is taken.

    B is held up by A exactly as in the first test, and still both are admitted: what B read before
    it waited was true when it read it and stale when it was used. Under READ COMMITTED only a read
    that starts after the lock is held sees what the previous holder committed, which is why the
    service reads after locking. This asserts the breach.
    """
    pair_lock = advisory_key("pair", medicine.id, PATIENT)
    first = start_a(clients)
    held.wait_until_inside()

    log.record("B starts")
    second = start_b(clients)
    wait_for_waiters(
        racing_engine, "waiting for the patient's lock", waiting_for_advisory_lock(pair_lock)
    )
    log.record("B is seen waiting")

    log.record("A is released")
    held.release()
    responses = [answer(first), answer(second)]

    assert [response.status_code for response in responses] == [201, 201]
    assert sum(recorded_quantities(ledger)) == 2 * REQUESTED
    assert sum(recorded_quantities(ledger)) > CAP
    assert log.events == [
        "A read a history of []",
        "B starts",
        "B is seen waiting",
        "A is released",
        "B read a history of []",
    ]
