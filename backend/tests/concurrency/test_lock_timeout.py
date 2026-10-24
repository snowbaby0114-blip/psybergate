"""A request that has to wait for a lock longer than LOCK_TIMEOUT_MS is a 503 resource_busy with
Retry-After, and it leaves nothing behind: no dispense and no attempt, so that the client can send
it again with the same idempotency key as if it had never been sent.
"""

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app.models import FormularyRule, Medicine
from tests.api.dispense_support import REPLAYED, Ledger, hold_advisory_lock
from tests.api.support import assert_problem
from tests.concurrency.support import (
    FITTING,
    DatabaseError,
    DatabaseErrors,
    assert_the_quota_held,
    race,
    requests_for,
    send,
)

pytestmark = pytest.mark.concurrency

LOCK_TIMEOUT_MS = 100
SQLSTATE_LOCK_NOT_AVAILABLE = "55P03"
WAITING = 8


@pytest.fixture
def app(build_app: Callable[..., FastAPI]) -> FastAPI:
    return build_app(lock_timeout_ms=LOCK_TIMEOUT_MS)


def test_a_request_that_waits_for_the_patient_longer_than_the_lock_timeout_leaves_nothing(
    new_client: Callable[[], TestClient],
    racing_sessions: sessionmaker[Session],
    medicine: Medicine,
    rule: FormularyRule,
    ledger: Ledger,
    db_errors: DatabaseErrors,
) -> None:
    with hold_advisory_lock(racing_sessions, "pair", medicine.id, "PT-1"):
        busy = send(new_client(), idempotency_key="key-1", quantity=30)

    assert_problem(busy, 503, "resource_busy")
    assert busy.headers["retry-after"] == "1"
    assert (ledger.dispenses(), ledger.attempts()) == ([], [])
    assert db_errors.seen == [DatabaseError(SQLSTATE_LOCK_NOT_AVAILABLE, None)]


def test_requests_held_up_under_load_are_all_503s_and_can_all_be_sent_again(
    new_client: Callable[[], TestClient],
    racing_sessions: sessionmaker[Session],
    medicine: Medicine,
    rule: FormularyRule,
    ledger: Ledger,
    db_errors: DatabaseErrors,
) -> None:
    """Eight requests for one patient arrive while something else holds the patient's lock. None
    gets through, none leaves a trace, and a patient or a medicine that is not involved is not held
    up. Sent again with the same keys once the lock is free, they are decided as if they were new:
    three of them fit."""
    pair = (medicine, "PT-1")
    other_patient = (medicine, "PT-2")

    with hold_advisory_lock(racing_sessions, "pair", medicine.id, "PT-1"):
        busy = race(requests_for(new_client, pair, WAITING))
        unaffected = race(requests_for(new_client, other_patient, 1))

    assert [response.status_code for response in busy] == [503] * WAITING
    for response in busy:
        assert_problem(response, 503, "resource_busy")
        assert response.headers["retry-after"] == "1"
    assert [response.status_code for response in unaffected] == [201]
    assert [dispense.patient_ref for dispense in ledger.dispenses()] == ["PT-2"]
    assert [attempt.patient_ref for attempt in ledger.attempts()] == ["PT-2"]
    assert {error.sqlstate for error in db_errors.seen} == {SQLSTATE_LOCK_NOT_AVAILABLE}

    again = race(requests_for(new_client, pair, WAITING))

    assert_the_quota_held(again, ledger, pair, fitting=FITTING)
    assert not [response for response in again if REPLAYED in response.headers]
