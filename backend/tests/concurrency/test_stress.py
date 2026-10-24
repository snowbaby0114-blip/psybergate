"""Rule 9 under load: many requests for the same patient and medicine, started together.

Every race has the same arithmetic. The cap is 100 per 30 days, each request asks for 30, so
exactly three fit whatever the order: three accepted, the rest rejected, 90 dispensed, and one
attempt row per request. That has to hold in every interleaving, which is what makes it a check
of the lock and not of luck. How many requests were really in flight together is not left to
chance in `test_sixteen_requests_are_all_in_flight_together`, which waits for the monitor to see
them all queued before it lets the first one go.
"""

from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta
from functools import partial
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app import repositories
from app.domain.timeutil import to_local_date, window_start_date
from app.locking import advisory_key
from app.models import FormularyRule, Medicine
from tests.api.dispense_support import Ledger
from tests.concurrency.monitor import (
    advisory_lock_holders,
    wait_for_waiters,
    waiting_for_advisory_lock,
)
from tests.concurrency.support import (
    CAP,
    FITTING,
    QUANTITY,
    QUOTA_EXCEEDED,
    DatabaseErrors,
    Gate,
    LiveServer,
    MedicineFactory,
    Pair,
    RuleFactory,
    answer,
    assert_the_quota_held,
    race,
    requests_for,
    send,
    start,
    violation_codes,
)

pytestmark = pytest.mark.concurrency

REQUESTS = 16


@pytest.mark.parametrize("round_number", [1, 2, 3, 4])
def test_sixteen_simultaneous_requests_admit_exactly_three(
    new_client: Callable[[], TestClient],
    medicine: Medicine,
    rule: FormularyRule,
    ledger: Ledger,
    db_errors: DatabaseErrors,
    round_number: int,
) -> None:
    pair = (medicine, f"PT-ROUND-{round_number}")

    responses = race(requests_for(new_client, pair, REQUESTS))

    assert_the_quota_held(responses, ledger, pair)
    assert db_errors.seen == []


def test_the_same_race_through_a_real_server(
    live_server: LiveServer,
    medicine: Medicine,
    rule: FormularyRule,
    ledger: Ledger,
    db_errors: DatabaseErrors,
) -> None:
    pair = (medicine, "PT-LIVE")

    responses = race(requests_for(live_server.new_client, pair, REQUESTS))

    assert_the_quota_held(responses, ledger, pair)
    assert db_errors.seen == []


def test_sixteen_requests_are_all_in_flight_together(
    new_client: Callable[[], TestClient],
    hold: Callable[..., Gate],
    racing_engine: Engine,
    medicine: Medicine,
    rule: FormularyRule,
    ledger: Ledger,
    db_errors: DatabaseErrors,
) -> None:
    """The first request is held inside its transaction until the monitor has seen the other
    fifteen queued on its lock, so all sixteen are provably in flight at once."""
    pair = (medicine, "PT-QUEUE")
    pair_lock = advisory_key("pair", medicine.id, pair[1])
    held = hold(repositories, "pair_history")
    first, *others = requests_for(new_client, pair, REQUESTS)
    started = [start(first)]
    held.wait_until_inside()

    started += [start(call) for call in others]
    queued = wait_for_waiters(
        racing_engine,
        "queued on the patient's lock",
        waiting_for_advisory_lock(pair_lock),
        count=REQUESTS - 1,
    )
    holders = advisory_lock_holders(racing_engine, pair_lock)
    held.release()
    responses = [answer(request) for request in started]

    assert len(holders) == 1
    assert all(holders <= set(waiter.blocked_by) for waiter in queued)
    assert_the_quota_held(responses, ledger, pair)
    assert db_errors.seen == []


class TestSeveralPairsAtOnce:
    @pytest.fixture
    def pairs(
        self,
        medicine: Medicine,
        rule: FormularyRule,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
    ) -> list[Pair]:
        """Two patients, each with two medicines, which have the same limits."""
        other = make_medicine()
        make_rule(other, date(2026, 1, 1))
        return [(m, p) for m in (medicine, other) for p in ("PT-1", "PT-2")]

    def test_interleaved_requests_for_four_pairs_are_decided_pair_by_pair(
        self,
        new_client: Callable[[], TestClient],
        pairs: list[Pair],
        ledger: Ledger,
        db_errors: DatabaseErrors,
    ) -> None:
        per_pair = 6
        by_pair = [requests_for(new_client, pair, per_pair, quantity=40) for pair in pairs]
        calls = [call for round_of_calls in zip(*by_pair, strict=True) for call in round_of_calls]

        responses = race(calls)

        # Responses come back in the order of the calls: pair 0, 1, 2, 3, 0, 1, 2, 3, ...
        for index, pair in enumerate(pairs):
            assert_the_quota_held(
                responses[index :: len(pairs)], ledger, pair, fitting=2, quantity=40
            )
        assert db_errors.seen == []

    def test_a_pair_that_is_held_up_holds_up_none_of_the_others(
        self,
        new_client: Callable[[], TestClient],
        hold: Callable[..., Gate],
        racing_engine: Engine,
        pairs: list[Pair],
        ledger: Ledger,
        db_errors: DatabaseErrors,
    ) -> None:
        """One request is held inside its transaction, holding the lock of its patient and
        medicine. A second request for the same pair waits behind it, as it must. Requests for
        the pair that shares its patient, the pair that shares its medicine and the pair that
        shares neither are all decided, two of them each, while it is still held."""
        held_pair, *others = pairs
        held_medicine, held_patient = held_pair

        def is_the_held_pair(session: Session, **query: Any) -> bool:
            return (query["medicine_id"], query["patient_ref"]) == (held_medicine.id, held_patient)

        held = hold(repositories, "pair_history", only_if=is_the_held_pair)
        first, second = requests_for(new_client, held_pair, 2, quantity=60)
        held_first = start(first)
        held.wait_until_inside()
        held_second = start(second)
        wait_for_waiters(
            racing_engine,
            "waiting behind the held request",
            waiting_for_advisory_lock(advisory_key("pair", held_medicine.id, held_patient)),
        )

        decided = {pair: race(requests_for(new_client, pair, 2, quantity=60)) for pair in others}

        assert not held_second.done()
        for pair, responses in decided.items():
            assert_the_quota_held(responses, ledger, pair, fitting=1, quantity=60)
        held.release()
        assert_the_quota_held(
            [answer(held_first), answer(held_second)], ledger, held_pair, fitting=1, quantity=60
        )
        assert db_errors.seen == []


def test_backdated_requests_that_race_keep_every_recorded_window_within_the_cap(
    new_client: Callable[[], TestClient],
    medicine: Medicine,
    rule: FormularyRule,
    ledger: Ledger,
    db_errors: DatabaseErrors,
) -> None:
    """Eight requests dated on eight different days, all inside one window, race in any order.
    A backdated request must also be counted by the windows of the later dispenses, so the check
    is on every recorded dispense: the quantities in its own 30 days are within the cap."""
    first_day = date(2026, 3, 10)
    calls = [
        partial(
            send,
            new_client(),
            idempotency_key=f"day-{number}",
            patient_ref="PT-BACKDATED",
            quantity=QUANTITY,
            dispensed_at=f"{first_day + timedelta(days=number):%Y-%m-%d}T10:00:00Z",
        )
        for number in range(8)
    ]

    responses = race(calls)

    accepted = Counter(response.status_code for response in responses)
    assert accepted == {201: FITTING, 422: 8 - FITTING}
    for rejected in (response for response in responses if response.status_code == 422):
        assert set(violation_codes(rejected)) <= {QUOTA_EXCEEDED, "later_dispense_limit_exceeded"}
    recorded = [
        (to_local_date(dispense.dispensed_at), dispense.quantity) for dispense in ledger.dispenses()
    ]
    for day, _ in recorded:
        in_its_window = sum(
            quantity
            for other_day, quantity in recorded
            if window_start_date(day) <= other_day <= day
        )
        assert in_its_window <= CAP
    assert len(ledger.attempts()) == len(responses)
    assert db_errors.seen == []
