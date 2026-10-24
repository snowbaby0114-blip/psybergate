"""Rule changes racing each other and racing dispenses.

A change of rules locks the medicine row FOR UPDATE and a dispense locks it FOR SHARE, so changes
queue one behind another, and a dispense and a change queue one behind the other, whichever came
first. What queues is checked through the monitor, and what the one that waited then does is
checked against what the other committed: a change plans on the rules the previous change left, a
dispense is judged by the rule the change introduced, and a change that would re-govern a
recorded dispense finds it.

The exclusion constraint on rule periods is the backstop. These races are decided before a
statement could reach it, so none of them may leave a database error behind.
"""

from collections import Counter
from collections.abc import Callable
from datetime import date
from functools import partial

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app import repositories
from app.models import FormularyRule, Medicine
from tests.api.dispense_support import Ledger
from tests.api.support import assert_problem
from tests.concurrency.monitor import wait_for_waiters, waiting_for_medicine_row
from tests.concurrency.support import (
    DatabaseErrors,
    Gate,
    answer,
    race,
    send,
    send_rule,
    start,
    violation_codes,
)

pytestmark = pytest.mark.concurrency

JANUARY = date(2026, 1, 1)
JULY = date(2026, 7, 1)
SEPTEMBER = date(2026, 9, 1)

Timeline = list[tuple[date, date | None]]

# Rules that overlap, and rules whose end is not the start of another: a timeline that is neither
# has no overlap and no gap.
_OVERLAPPING_PAIRS = text(
    """
    SELECT count(*) FROM formulary_rules a JOIN formulary_rules b
      ON a.medicine_id = b.medicine_id AND a.id < b.id
     AND daterange(a.effective_from, a.effective_to, '[)')
      && daterange(b.effective_from, b.effective_to, '[)')
    """
)
_RULES_ENDING_IN_A_GAP = text(
    """
    SELECT count(*) FROM formulary_rules r
    WHERE r.effective_to IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM formulary_rules n
        WHERE n.medicine_id = r.medicine_id AND n.effective_from = r.effective_to
    )
    """
)


def timeline_of(racing_sessions: sessionmaker[Session], medicine: Medicine) -> Timeline:
    stmt = (
        select(FormularyRule.effective_from, FormularyRule.effective_to)
        .where(FormularyRule.medicine_id == medicine.id)
        .order_by(FormularyRule.effective_from)
    )
    with racing_sessions() as session:
        return [(begin, end) for begin, end in session.execute(stmt)]


def assert_no_overlap_and_no_gap(racing_sessions: sessionmaker[Session]) -> None:
    with racing_sessions() as session:
        assert session.scalar(_OVERLAPPING_PAIRS) == 0
        assert session.scalar(_RULES_ENDING_IN_A_GAP) == 0


def tiling(*starts: date) -> Timeline:
    """What the rule history is after the rules that start on `starts` were introduced over the
    open-ended rule of 1 January: each period ends where the next begins."""
    boundaries = [JANUARY, *sorted(starts)]
    return [(begin, end) for begin, end in zip(boundaries, [*boundaries[1:], None], strict=True)]


@pytest.fixture
def clients(new_client: Callable[[], TestClient]) -> Callable[[int], list[TestClient]]:
    return lambda count: [new_client() for _ in range(count)]


class TestTheSameDate:
    @pytest.mark.parametrize("racers", [2, 6])
    def test_of_simultaneous_supersedes_exactly_one_succeeds_and_the_others_conflict(
        self,
        clients: Callable[[int], list[TestClient]],
        racing_sessions: sessionmaker[Session],
        medicine: Medicine,
        rule: FormularyRule,
        db_errors: DatabaseErrors,
        racers: int,
    ) -> None:
        responses = race(
            [partial(send_rule, client, medicine.code, SEPTEMBER) for client in clients(racers)]
        )

        assert Counter(response.status_code for response in responses) == {
            201: 1,
            409: racers - 1,
        }
        for conflict in (response for response in responses if response.status_code == 409):
            assert_problem(conflict, 409, "rule_conflict")
            assert violation_codes(conflict) == ["rule_already_starts_on_date"]
        assert timeline_of(racing_sessions, medicine) == tiling(SEPTEMBER)
        assert db_errors.seen == []

    def test_the_second_waits_for_the_medicine_and_then_finds_the_date_taken(
        self,
        clients: Callable[[int], list[TestClient]],
        hold: Callable[..., Gate],
        racing_engine: Engine,
        racing_sessions: sessionmaker[Session],
        medicine: Medicine,
        rule: FormularyRule,
        db_errors: DatabaseErrors,
    ) -> None:
        """The first supersede is held after it has inserted its rule and before it commits."""
        held = hold(repositories, "add_rule")
        client_a, client_b = clients(2)
        first = start(partial(send_rule, client_a, medicine.code, SEPTEMBER))
        held.wait_until_inside()
        second = start(partial(send_rule, client_b, medicine.code, SEPTEMBER))
        wait_for_waiters(
            racing_engine,
            "waiting to lock the medicine for update",
            waiting_for_medicine_row("UPDATE"),
        )
        held.release()
        created, conflict = answer(first), answer(second)

        assert created.status_code == 201
        assert_problem(conflict, 409, "rule_conflict")
        assert violation_codes(conflict) == ["rule_already_starts_on_date"]
        assert timeline_of(racing_sessions, medicine) == tiling(SEPTEMBER)
        assert db_errors.seen == []


class TestDifferentDates:
    @pytest.mark.parametrize("racers", [2, 6])
    def test_simultaneous_supersedes_all_succeed_and_the_timeline_tiles(
        self,
        clients: Callable[[int], list[TestClient]],
        racing_sessions: sessionmaker[Session],
        medicine: Medicine,
        rule: FormularyRule,
        db_errors: DatabaseErrors,
        racers: int,
    ) -> None:
        dates = [date(2026, 7 + number, 1) for number in range(racers)]

        responses = race(
            [
                partial(send_rule, client, medicine.code, day)
                for client, day in zip(clients(racers), dates, strict=True)
            ]
        )

        assert [response.status_code for response in responses] == [201] * racers
        assert timeline_of(racing_sessions, medicine) == tiling(*dates)
        assert_no_overlap_and_no_gap(racing_sessions)
        assert db_errors.seen == []

    @pytest.mark.parametrize(
        ("first_date", "second_date"),
        [(SEPTEMBER, JULY), (JULY, SEPTEMBER)],
        ids=["the later date first", "the earlier date first"],
    )
    def test_the_second_plans_on_the_rules_the_first_committed(
        self,
        clients: Callable[[int], list[TestClient]],
        hold: Callable[..., Gate],
        racing_engine: Engine,
        racing_sessions: sessionmaker[Session],
        medicine: Medicine,
        rule: FormularyRule,
        db_errors: DatabaseErrors,
        first_date: date,
        second_date: date,
    ) -> None:
        """The first supersede is held after it has inserted its rule and before it commits. The
        second one has to wait, and then plans on rules that include the first one's: planned on
        the open-ended rule it would have found there before, it would overlap."""
        held = hold(repositories, "add_rule")
        client_a, client_b = clients(2)
        first = start(partial(send_rule, client_a, medicine.code, first_date))
        held.wait_until_inside()
        second = start(partial(send_rule, client_b, medicine.code, second_date))
        wait_for_waiters(
            racing_engine,
            "waiting to lock the medicine for update",
            waiting_for_medicine_row("UPDATE"),
        )
        held.release()
        responses = [answer(first), answer(second)]

        assert [response.status_code for response in responses] == [201, 201]
        assert timeline_of(racing_sessions, medicine) == tiling(JULY, SEPTEMBER)
        assert_no_overlap_and_no_gap(racing_sessions)
        assert db_errors.seen == []


class TestADispenseAndASupersede:
    def test_a_dispense_in_flight_holds_up_a_retroactive_supersede_which_then_finds_it(
        self,
        clients: Callable[[int], list[TestClient]],
        hold: Callable[..., Gate],
        racing_engine: Engine,
        racing_sessions: sessionmaker[Session],
        medicine: Medicine,
        rule: FormularyRule,
        ledger: Ledger,
        db_errors: DatabaseErrors,
    ) -> None:
        """The dispense is dated 15 March and held after it has read its history, holding the
        medicine FOR SHARE. A rule from 1 March would govern it, so the supersede may not run
        before the dispense is on record: it waits, and then refuses."""
        held = hold(repositories, "pair_history")
        dispense_client, supersede_client = clients(2)
        dispensing = start(partial(send, dispense_client, idempotency_key="key-1", quantity=5))
        held.wait_until_inside()
        superseding = start(partial(send_rule, supersede_client, medicine.code, date(2026, 3, 1)))
        [waiter] = wait_for_waiters(
            racing_engine,
            "waiting to lock the medicine for update",
            waiting_for_medicine_row("UPDATE"),
        )
        still_waiting = not superseding.done()
        held.release()
        dispensed, refused = answer(dispensing), answer(superseding)

        assert still_waiting
        assert waiter.blocked_by
        assert dispensed.status_code == 201
        assert_problem(refused, 409, "rule_conflict")
        assert violation_codes(refused) == ["rule_would_alter_recorded_dispenses"]
        assert refused.json()["violations"][0]["context"]["dispense_count"] == 1
        assert [dispense.rule_id for dispense in ledger.dispenses()] == [rule.id]
        assert timeline_of(racing_sessions, medicine) == [(JANUARY, None)]
        assert db_errors.seen == []

    def test_a_supersede_in_flight_holds_up_a_dispense_which_is_then_judged_by_the_new_rule(
        self,
        clients: Callable[[int], list[TestClient]],
        hold: Callable[..., Gate],
        racing_engine: Engine,
        medicine: Medicine,
        rule: FormularyRule,
        ledger: Ledger,
        db_errors: DatabaseErrors,
    ) -> None:
        """The supersede introduces, from 1 March, a rule of at most 10 per dispense, and is held
        after it has inserted it, holding the medicine FOR UPDATE. A dispense of 20 on 15 March is
        within the old rule's 100 and over the new one's 10: it has to wait for the supersede and
        is rejected under the rule that the supersede introduced."""
        held = hold(repositories, "add_rule")
        dispense_client, supersede_client = clients(2)
        superseding = start(
            partial(
                send_rule,
                supersede_client,
                medicine.code,
                date(2026, 3, 1),
                max_quantity_per_dispense=10,
            )
        )
        held.wait_until_inside()
        dispensing = start(partial(send, dispense_client, idempotency_key="key-1", quantity=20))
        wait_for_waiters(
            racing_engine,
            "waiting to lock the medicine for share",
            waiting_for_medicine_row("SHARE"),
        )
        still_waiting = not dispensing.done()
        held.release()
        introduced, rejected = answer(superseding), answer(dispensing)

        assert still_waiting
        assert introduced.status_code == 201
        problem = assert_problem(
            rejected, 422, "dispense_rejected", extra_keys=frozenset({"attempt_id"})
        )
        [violation] = problem["violations"]
        assert violation["code"] == "max_quantity_per_dispense_exceeded"
        assert violation["context"] == {
            "limit": 10,
            "requested": 20,
            "rule_id": introduced.json()["rule"]["id"],
        }
        assert ledger.dispenses() == []
        [attempt] = ledger.attempts()
        assert attempt.outcome == "rejected"
        assert db_errors.seen == []
