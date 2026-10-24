"""Concurrent writers cannot create overlapping rule periods, with no application lock involved.

Everything here is raw SQL on separate connections (separate backends, separate transactions):
no advisory lock, no SELECT ... FOR UPDATE, no service code. If overlap is impossible here, it
is impossible whatever the application does.

The choreography never relies on timing. The first transaction inserts its period and stays
open; the second INSERT is started on another thread; the test then polls pg_stat_activity until
PostgreSQL itself reports that backend as waiting for a lock held by the first one
(wait_event_type = 'Lock', blocked by exactly that backend). Only then is the first transaction
ended. A test that merely happened to serialise could not make that observation.
"""

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date

import psycopg
import pytest
from psycopg import errors

from tests.db.support import connect, insert_medicine, insert_rule, wait_until

pytestmark = pytest.mark.concurrency

# Every round uses a fresh medicine; repeating shows the observation is not a one-off accident.
ROUNDS = range(5)
CONSTRAINT = "ex_formulary_rules_no_overlap"

FIRST = (date(2026, 1, 1), date(2026, 6, 1))
OVERLAPPING = (date(2026, 3, 1), None)
Period = tuple[date, date | None]
OpenConnection = Callable[..., psycopg.Connection]


@pytest.fixture
def open_connection(database_url: str, clean_db: None) -> Iterator[OpenConnection]:
    """Opens connections to the test database. A lock_timeout turns a bug that would block
    forever into a clear failure. All connections are closed at teardown, which also rolls back
    anything still open, so no lock outlives its test."""
    opened: list[psycopg.Connection] = []

    def open_new(*, autocommit: bool = False) -> psycopg.Connection:
        connection = connect(database_url, autocommit=autocommit, options="-c lock_timeout=10s")
        opened.append(connection)
        return connection

    yield open_new
    for connection in opened:
        connection.close()


@pytest.fixture
def monitor(open_connection: OpenConnection) -> psycopg.Connection:
    """Autocommit, so every query sees the current state of the server."""
    return open_connection(autocommit=True)


@pytest.fixture
def medicine(monitor: psycopg.Connection) -> int:
    """A committed medicine, distinct for every test and round."""
    return insert_medicine(monitor)


def is_blocked_by(
    monitor: psycopg.Connection, waiter: psycopg.Connection, blocker: psycopg.Connection
) -> bool:
    """PostgreSQL reports ``waiter`` as waiting for a transaction lock held by ``blocker``."""
    row = monitor.execute(
        "SELECT wait_event_type, wait_event, pg_blocking_pids(pid)"
        " FROM pg_stat_activity WHERE pid = %s",
        (waiter.info.backend_pid,),
    ).fetchone()
    return row == ("Lock", "transactionid", [blocker.info.backend_pid])


def committed_periods(monitor: psycopg.Connection, medicine: int) -> list[Period]:
    return monitor.execute(
        "SELECT effective_from, effective_to FROM formulary_rules"
        " WHERE medicine_id = %s ORDER BY effective_from",
        (medicine,),
    ).fetchall()


def start_blocked_insert(
    pool: ThreadPoolExecutor,
    monitor: psycopg.Connection,
    medicine: int,
    first: psycopg.Connection,
    second: psycopg.Connection,
    period: Period,
) -> Future[int]:
    """Insert ``period`` on ``second`` (in another thread) and wait, observing PostgreSQL, until
    it is blocked by the open transaction on ``first``. Returns the future of that INSERT."""
    attempt = pool.submit(insert_rule, second, medicine, *period)
    # An INSERT that has already finished was not blocked by anything: stop waiting for it.
    wait_until(lambda: attempt.done() or is_blocked_by(monitor, second, first))
    assert not attempt.done(), "the second INSERT did not wait for the first transaction"
    assert is_blocked_by(monitor, second, first), "the second INSERT never showed up as waiting"
    return attempt


@pytest.mark.parametrize("round_number", ROUNDS)
def test_second_overlapping_insert_blocks_until_the_first_commits_and_then_fails(
    open_connection: OpenConnection, monitor: psycopg.Connection, medicine: int, round_number: int
) -> None:
    first, second = open_connection(), open_connection()
    insert_rule(first, medicine, *FIRST)  # not committed yet

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            attempt = start_blocked_insert(pool, monitor, medicine, first, second, OVERLAPPING)
        finally:
            first.commit()  # always release the second INSERT, even if the observation failed
        with pytest.raises(errors.ExclusionViolation) as raised:
            attempt.result(timeout=10)

    assert raised.value.sqlstate == "23P01"
    assert raised.value.diag.constraint_name == CONSTRAINT
    assert committed_periods(monitor, medicine) == [FIRST]


@pytest.mark.parametrize("round_number", ROUNDS)
def test_when_the_first_rolls_back_the_waiting_insert_succeeds(
    open_connection: OpenConnection, monitor: psycopg.Connection, medicine: int, round_number: int
) -> None:
    first, second = open_connection(), open_connection()
    insert_rule(first, medicine, *FIRST)  # never committed

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            attempt = start_blocked_insert(pool, monitor, medicine, first, second, OVERLAPPING)
        finally:
            first.rollback()
        attempt.result(timeout=10)
    second.commit()

    assert committed_periods(monitor, medicine) == [OVERLAPPING]


@pytest.mark.parametrize(
    ("same_medicine", "period"),
    [(True, (date(2026, 6, 1), None)), (False, FIRST)],
    ids=["adjacent period of the same medicine", "the very same dates for another medicine"],
)
def test_inserts_that_do_not_conflict_never_wait_for_each_other(
    open_connection: OpenConnection,
    monitor: psycopg.Connection,
    medicine: int,
    same_medicine: bool,
    period: Period,
) -> None:
    other_medicine = insert_medicine(monitor)
    first, second = open_connection(), open_connection()
    insert_rule(first, medicine, *FIRST)  # still uncommitted while the second one is inserted

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            target = medicine if same_medicine else other_medicine
            pool.submit(insert_rule, second, target, *period).result(timeout=5)
        finally:
            first.commit()
    second.commit()

    assert committed_periods(monitor, medicine) == ([FIRST, period] if same_medicine else [FIRST])
    assert committed_periods(monitor, other_medicine) == ([] if same_medicine else [period])


@pytest.mark.parametrize("round_number", range(3))
def test_simultaneous_overlapping_inserts_leave_exactly_one_winner_per_conflict(
    open_connection: OpenConnection, monitor: psycopg.Connection, medicine: int, round_number: int
) -> None:
    """Six writers released together by a barrier, in two groups. Within a group every period
    overlaps every other; the groups do not overlap each other. Whatever the interleaving,
    each group must end with exactly one committed period.

    A loser may be told 23P01 (it saw the winner) or 40P01: with truly simultaneous inserts two
    transactions can each wait for the other's uncommitted row, and PostgreSQL's deadlock
    detector then aborts one of them. Either way nothing overlapping is ever committed."""
    first_half = [(date(2026, 1, day), date(2026, 3, 1)) for day in (1, 2, 3)]
    second_half = [(date(2026, 7, day), date(2026, 9, 1)) for day in (1, 2, 3)]
    periods = first_half + second_half
    connections = [open_connection(autocommit=True) for _ in periods]
    barrier = threading.Barrier(len(periods))

    def insert(connection: psycopg.Connection, period: Period) -> str:
        barrier.wait(timeout=10)
        try:
            insert_rule(connection, medicine, *period)
        except psycopg.Error as error:
            return str(error.sqlstate)
        return "inserted"

    with ThreadPoolExecutor(max_workers=len(periods)) as pool:
        outcomes = list(pool.map(insert, connections, periods, timeout=60))

    assert outcomes.count("inserted") == 2
    assert set(outcomes) <= {"inserted", "23P01", "40P01"}
    winners = committed_periods(monitor, medicine)
    assert len(winners) == 2
    assert winners[0] in first_half
    assert winners[1] in second_half
