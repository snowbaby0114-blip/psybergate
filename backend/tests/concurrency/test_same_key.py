"""Rule 10 under concurrency: requests that carry one idempotency key, arriving together, record
one outcome and all get it back; a payload that differs from the first one's is an error.

The key lock queues the requests that share a key, so the later ones find the first one's attempt
and replay it. The unique constraints on the key are the second line of defence for a request
that missed it, and the tests without the key lock show that they hold on their own.
"""

import json
from collections import Counter
from collections.abc import Callable
from datetime import date
from functools import partial

import pytest
from httpx import Response
from sqlalchemy import Engine
from starlette.testclient import TestClient

from app import locking, repositories
from app.locking import advisory_key
from app.models import FormularyRule, Medicine
from tests.api.dispense_support import REPLAYED, Ledger
from tests.api.support import assert_problem
from tests.concurrency.monitor import wait_for_waiters, waiting_for_advisory_lock
from tests.concurrency.support import (
    DatabaseError,
    DatabaseErrors,
    Gate,
    RuleFactory,
    answer,
    race,
    send,
    start,
)

pytestmark = pytest.mark.concurrency

KEY = "same-key"
DUPLICATES = 8
# Within the 100 per dispense of the rule, and over it.
ACCEPTED, REJECTED = 60, 101
ATTEMPT_UNIQUE = "uq_dispense_attempts_idempotency_key"
DISPENSE_UNIQUE = "uq_dispenses_idempotency_key"


def outcome(response: Response) -> tuple[int, str]:
    """Status and body, apart from the id of the request that was answered, which is always
    another one."""
    body = {name: value for name, value in response.json().items() if name != "request_id"}
    return response.status_code, json.dumps(body, sort_keys=True)


def replays(responses: list[Response]) -> int:
    return sum(REPLAYED in response.headers for response in responses)


def assert_one_outcome_for_all(responses: list[Response], ledger: Ledger, *, status: int) -> None:
    """Every response is the same outcome, which was decided once and replayed for all others."""
    assert len({outcome(response) for response in responses}) == 1
    assert {response.status_code for response in responses} == {status}
    assert replays(responses) == len(responses) - 1
    assert len(ledger.attempts()) == 1
    assert len(ledger.dispenses()) == (1 if status == 201 else 0)


@pytest.fixture
def identical(
    new_client: Callable[[], TestClient],
) -> Callable[..., list[Callable[[], Response]]]:
    def requests(count: int, **changes: object) -> list[Callable[[], Response]]:
        return [partial(send, new_client(), idempotency_key=KEY, **changes) for _ in range(count)]

    return requests


@pytest.mark.parametrize(
    ("quantity", "status"), [(ACCEPTED, 201), (REJECTED, 422)], ids=["accepted", "rejected"]
)
class TestIdenticalRequests:
    def test_eight_simultaneous_duplicates_record_one_outcome_and_get_it_back(
        self,
        identical: Callable[..., list[Callable[[], Response]]],
        rule: FormularyRule,
        ledger: Ledger,
        db_errors: DatabaseErrors,
        quantity: int,
        status: int,
    ) -> None:
        responses = race(identical(DUPLICATES, quantity=quantity))

        assert_one_outcome_for_all(responses, ledger, status=status)
        # The key lock queued them: none of the duplicates had to be stopped by a constraint.
        assert db_errors.seen == []

    def test_duplicates_queue_on_the_key_lock_and_replay_the_first_outcome(
        self,
        identical: Callable[..., list[Callable[[], Response]]],
        hold: Callable[..., Gate],
        racing_engine: Engine,
        rule: FormularyRule,
        ledger: Ledger,
        db_errors: DatabaseErrors,
        quantity: int,
        status: int,
    ) -> None:
        """The first request is held inside its transaction until the monitor has seen the other
        seven queued on its idempotency key, so all eight are provably in flight together."""
        held = hold(repositories, "pair_history")
        first, *others = identical(DUPLICATES, quantity=quantity)
        started = [start(first)]
        held.wait_until_inside()

        started += [start(call) for call in others]
        wait_for_waiters(
            racing_engine,
            "queued on the idempotency key",
            waiting_for_advisory_lock(advisory_key("idem", KEY)),
            count=DUPLICATES - 1,
        )
        held.release()
        responses = [answer(request) for request in started]

        assert_one_outcome_for_all(responses, ledger, status=status)
        assert REPLAYED not in responses[0].headers
        assert db_errors.seen == []


class TestWithoutTheKeyLock:
    """The unique constraints alone. With locking.lock_idempotency_key taking no lock, duplicates
    do not queue on their key: they miss the first request's attempt, wait for the patient's lock
    instead, decide again after it commits, and are stopped when they record their outcome."""

    @pytest.fixture(autouse=True)
    def _no_key_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(locking, "lock_idempotency_key", lambda *args, **kwargs: None)

    @pytest.mark.parametrize(
        ("cap", "constraint"),
        [(100, ATTEMPT_UNIQUE), (1000, DISPENSE_UNIQUE)],
        ids=[
            "the duplicate is rejected on the history",
            "the duplicate is accepted on the history",
        ],
    )
    def test_the_duplicate_is_stopped_by_the_constraint_and_answered_with_the_first_outcome(
        self,
        new_client: Callable[[], TestClient],
        hold: Callable[..., Gate],
        racing_engine: Engine,
        medicine: Medicine,
        make_rule: RuleFactory,
        ledger: Ledger,
        db_errors: DatabaseErrors,
        cap: int,
        constraint: str,
    ) -> None:
        """The first request is accepted and held before it commits, so the duplicate cannot find
        its attempt. After the first one commits, the duplicate reads a history that holds the
        first one's 60. With a cap of 100 that rejects it and it fails on the attempt's key, with
        a cap of 1000 it accepts it and fails on the dispense's key. Either way it is rolled back
        and answered with the committed outcome."""
        make_rule(medicine, date(2026, 1, 1), max_quantity_per_30_days=cap)
        pair_lock = advisory_key("pair", medicine.id, "PT-1")
        held = hold(repositories, "pair_history")
        client_a, client_b = new_client(), new_client()
        first = start(partial(send, client_a, idempotency_key=KEY, quantity=ACCEPTED))
        held.wait_until_inside()
        second = start(partial(send, client_b, idempotency_key=KEY, quantity=ACCEPTED))
        wait_for_waiters(
            racing_engine, "waiting for the patient's lock", waiting_for_advisory_lock(pair_lock)
        )
        held.release()
        responses = [answer(first), answer(second)]

        assert_one_outcome_for_all(responses, ledger, status=201)
        assert REPLAYED not in responses[0].headers
        assert db_errors.seen == [DatabaseError("23505", constraint)]

    def test_eight_simultaneous_duplicates_still_record_one_outcome(
        self,
        identical: Callable[..., list[Callable[[], Response]]],
        rule: FormularyRule,
        ledger: Ledger,
        db_errors: DatabaseErrors,
    ) -> None:
        responses = race(identical(DUPLICATES, quantity=ACCEPTED))

        assert_one_outcome_for_all(responses, ledger, status=201)
        assert {error.sqlstate for error in db_errors.seen} <= {"23505"}


class TestDifferentPayloadsUnderOneKey:
    def test_the_first_payload_wins_and_the_other_is_an_idempotency_key_reuse(
        self,
        new_client: Callable[[], TestClient],
        hold: Callable[..., Gate],
        racing_engine: Engine,
        rule: FormularyRule,
        ledger: Ledger,
        db_errors: DatabaseErrors,
    ) -> None:
        held = hold(repositories, "pair_history")
        first = start(partial(send, new_client(), idempotency_key=KEY, quantity=10))
        held.wait_until_inside()
        second = start(partial(send, new_client(), idempotency_key=KEY, quantity=20))
        wait_for_waiters(
            racing_engine,
            "queued on the idempotency key",
            waiting_for_advisory_lock(advisory_key("idem", KEY)),
        )
        held.release()
        accepted, refused = answer(first), answer(second)

        assert accepted.status_code == 201
        assert_problem(refused, 422, "idempotency_key_reuse")
        assert [dispense.quantity for dispense in ledger.dispenses()] == [10]
        assert len(ledger.attempts()) == 1
        assert db_errors.seen == []

    def test_whichever_payload_wins_the_race_the_others_are_replays_or_reuses(
        self,
        new_client: Callable[[], TestClient],
        rule: FormularyRule,
        ledger: Ledger,
        db_errors: DatabaseErrors,
    ) -> None:
        """Four requests with a quantity of 10 and four with 20 share a key. One payload wins,
        which one is up to the race: its four requests get the winner's outcome, three of them as
        replays, and the other four are refused."""
        quantities = [10, 20] * 4
        calls = [
            partial(send, new_client(), idempotency_key=KEY, quantity=quantity)
            for quantity in quantities
        ]

        responses = race(calls)

        [recorded] = ledger.dispenses()
        winners = [
            r
            for r, quantity in zip(responses, quantities, strict=True)
            if quantity == recorded.quantity
        ]
        others = [
            r
            for r, quantity in zip(responses, quantities, strict=True)
            if quantity != recorded.quantity
        ]
        assert_one_outcome_for_all(winners, ledger, status=201)
        for response in others:
            assert_problem(response, 422, "idempotency_key_reuse")
        assert Counter(response.status_code for response in responses) == {201: 4, 422: 4}
        assert len(ledger.attempts()) == 1
        assert db_errors.seen == []
