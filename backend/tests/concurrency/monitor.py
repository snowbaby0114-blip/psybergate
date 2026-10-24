"""Watching PostgreSQL from the outside: which backends wait for a lock, for which one, and behind
whom.

The concurrency tests never sleep and hope that two requests overlapped. They let a request go
only after a monitor connection has seen the other one blocked, so that the overlap is observed
instead of assumed.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, Engine, text

OBSERVE_TIMEOUT_SECONDS = 10.0
# How often the monitor looks again. The wait ends when the observation holds, never when time has
# passed, so this only bounds how late the test notices.
_POLL_SECONDS = 0.01

# The two halves of the 64-bit key of an advisory lock are the classid and the objid of its pg_locks
# row. `<<` wraps around on bigint, which turns the halves back into the signed key
# app.locking.advisory_key produced.
_KEY_OF_LOCK = "(l.classid::bigint << 32) | l.objid::bigint"
_IN_THIS_DATABASE = "l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"

_WAITING_FOR_A_LOCK = text(
    f"""
    SELECT a.pid, a.wait_event, a.query, pg_blocking_pids(a.pid) AS blocked_by,
           (SELECT {_KEY_OF_LOCK} FROM pg_locks l
             WHERE l.pid = a.pid AND l.locktype = 'advisory' AND NOT l.granted) AS advisory_key
    FROM pg_stat_activity a
    WHERE a.datname = current_database() AND a.wait_event_type = 'Lock'
    """
)

_HOLDING_AN_ADVISORY_LOCK = text(
    f"""
    SELECT l.pid FROM pg_locks l
    WHERE l.locktype = 'advisory' AND l.granted AND {_IN_THIS_DATABASE}
      AND ({_KEY_OF_LOCK}) = :key
    """
)

# A row lock that has to wait shows up as a wait for the transaction that holds the row, or for
# the queue in front of the row when several are waiting.
_ROW_LOCK_WAITS = ("transactionid", "tuple")


@dataclass(frozen=True)
class Waiter:
    """A backend of this database that is waiting for a lock."""

    pid: int
    wait_event: str
    query: str
    blocked_by: tuple[int, ...]
    advisory_key: int | None


Matcher = Callable[[Waiter], bool]


def waiting_for_advisory_lock(key: int) -> Matcher:
    """Waiting in pg_advisory_xact_lock for the lock `key`."""
    return lambda waiter: (
        waiter.wait_event == "advisory"
        and waiter.advisory_key == key
        and "pg_advisory_xact_lock" in waiter.query
    )


def waiting_for_medicine_row(lock: Literal["SHARE", "UPDATE"]) -> Matcher:
    """Waiting at the SELECT ... FOR SHARE (a dispense) or FOR UPDATE (a change of rules) that
    locks the row of a medicine."""
    return lambda waiter: (
        waiter.wait_event in _ROW_LOCK_WAITS
        and "FROM medicines" in waiter.query
        and waiter.query.rstrip().endswith(f"FOR {lock}")
    )


def _waiters(monitor: Connection) -> list[Waiter]:
    return [
        Waiter(pid, wait_event, query, tuple(blocked_by), advisory_key)
        for pid, wait_event, query, blocked_by, advisory_key in monitor.execute(_WAITING_FOR_A_LOCK)
    ]


def wait_for_waiters(
    engine: Engine,
    what: str,
    matching: Matcher,
    *,
    count: int = 1,
    timeout: float = OBSERVE_TIMEOUT_SECONDS,
) -> list[Waiter]:
    """Poll until `count` backends matching `matching` are seen waiting, and return them.

    Fails with everything that was seen waiting if that does not happen within `timeout`: it
    would mean that the request was not concurrent, or was not held up by the lock it is meant to
    be held up by.
    """
    deadline = time.monotonic() + timeout
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as monitor:
        while True:
            seen = _waiters(monitor)
            found = [waiter for waiter in seen if matching(waiter)]
            if len(found) >= count:
                return found
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"No {count} backend(s) {what} were seen within {timeout:g} s. "
                    f"Backends waiting for a lock: {seen or 'none'}"
                )
            time.sleep(_POLL_SECONDS)


def advisory_lock_holders(engine: Engine, key: int) -> set[int]:
    """The backends that hold the advisory lock `key` right now."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as monitor:
        return set(monitor.execute(_HOLDING_AN_ADVISORY_LOCK, {"key": key}).scalars())
