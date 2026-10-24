"""The tools of the concurrency tests, tested on their own.

A monitor that never sees anything, or an error recorder that records nothing, would make the
tests built on them pass for the wrong reason, so each is shown to see what it is there to see.
"""

import threading
from typing import Literal

import pytest
from httpx import Response
from sqlalchemy import Engine, exc, text
from sqlalchemy.orm import Session, sessionmaker

from app.locking import advisory_key
from app.models import Medicine
from tests.api.dispense_support import hold_advisory_lock, hold_medicine_row
from tests.concurrency.monitor import (
    advisory_lock_holders,
    wait_for_waiters,
    waiting_for_advisory_lock,
    waiting_for_medicine_row,
)
from tests.concurrency.support import DatabaseError, DatabaseErrors, race

pytestmark = pytest.mark.concurrency

SHORT_TIMEOUT = 0.2
JOIN_SECONDS = 10
RowLock = Literal["SHARE", "UPDATE"]


def wait_in_a_thread(
    racing_sessions: sessionmaker[Session], statement: str, **parameters: object
) -> threading.Thread:
    """Run `statement`, which has to wait for a lock the test holds, in a transaction on a thread
    of its own. The thread ends once the lock is let go; the test then joins it."""

    def run() -> None:
        with racing_sessions() as session, session.begin():
            session.execute(text(statement), parameters)

    waiter = threading.Thread(target=run, daemon=True)
    waiter.start()
    return waiter


class TestMonitor:
    TAKE_ADVISORY_LOCK = "SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"

    def test_names_the_advisory_lock_a_backend_waits_for_and_who_holds_it(
        self, racing_engine: Engine, racing_sessions: sessionmaker[Session]
    ) -> None:
        key = advisory_key("pair", 1, "PT-1")
        with hold_advisory_lock(racing_sessions, "pair", 1, "PT-1"):
            thread = wait_in_a_thread(racing_sessions, self.TAKE_ADVISORY_LOCK, key=key)
            [waiter] = wait_for_waiters(
                racing_engine, "waiting for the lock", waiting_for_advisory_lock(key)
            )
            holders = advisory_lock_holders(racing_engine, key)
        thread.join(JOIN_SECONDS)

        assert waiter.wait_event == "advisory"
        assert waiter.advisory_key == key
        assert len(holders) == 1
        assert set(waiter.blocked_by) == holders

    def test_does_not_mistake_a_wait_for_another_advisory_lock(
        self, racing_engine: Engine, racing_sessions: sessionmaker[Session]
    ) -> None:
        key = advisory_key("pair", 1, "PT-1")
        other = advisory_key("pair", 1, "PT-2")
        with hold_advisory_lock(racing_sessions, "pair", 1, "PT-1"):
            thread = wait_in_a_thread(racing_sessions, self.TAKE_ADVISORY_LOCK, key=key)
            wait_for_waiters(racing_engine, "waiting for the lock", waiting_for_advisory_lock(key))

            with pytest.raises(AssertionError, match="No 1 backend"):
                wait_for_waiters(
                    racing_engine,
                    "waiting for the other lock",
                    waiting_for_advisory_lock(other),
                    timeout=SHORT_TIMEOUT,
                )
        thread.join(JOIN_SECONDS)

    def test_fails_with_what_it_did_see_when_nobody_waits(self, racing_engine: Engine) -> None:
        with pytest.raises(AssertionError) as failure:
            wait_for_waiters(
                racing_engine,
                "waiting for the lock",
                waiting_for_advisory_lock(advisory_key("pair", 1, "PT-1")),
                timeout=SHORT_TIMEOUT,
            )

        assert "waiting for the lock were seen within 0.2 s" in str(failure.value)
        assert "Backends waiting for a lock: none" in str(failure.value)

    @pytest.mark.parametrize(
        ("held", "asked_for", "not_asked_for"),
        [("UPDATE", "SHARE", "UPDATE"), ("SHARE", "UPDATE", "SHARE")],
        ids=["a share waits for an update", "an update waits for a share"],
    )
    def test_tells_a_wait_for_the_row_of_a_medicine_by_the_lock_it_asks_for(
        self,
        racing_engine: Engine,
        racing_sessions: sessionmaker[Session],
        medicine: Medicine,
        held: RowLock,
        asked_for: RowLock,
        not_asked_for: RowLock,
    ) -> None:
        with hold_medicine_row(racing_sessions, medicine.id, held):
            thread = wait_in_a_thread(
                racing_sessions,
                f"SELECT id FROM medicines WHERE id = :id FOR {asked_for}",
                id=medicine.id,
            )
            [waiter] = wait_for_waiters(
                racing_engine,
                f"waiting for the medicine row FOR {asked_for}",
                waiting_for_medicine_row(asked_for),
            )
        thread.join(JOIN_SECONDS)

        assert not waiting_for_medicine_row(not_asked_for)(waiter)
        assert waiter.blocked_by


class TestDatabaseErrors:
    def test_records_the_sqlstate_and_the_constraint_of_what_postgresql_refused(
        self, racing_engine: Engine, db_errors: DatabaseErrors
    ) -> None:
        insert = text(
            "INSERT INTO medicines (code, name, form, strength_value, strength_unit) "
            "VALUES ('MED-DUP', 'n', 'f', 1, 'mg')"
        )

        with pytest.raises(exc.IntegrityError), racing_engine.begin() as connection:
            connection.execute(insert)
            connection.execute(insert)

        assert db_errors.seen == [DatabaseError("23505", "uq_medicines_code")]

    def test_has_nothing_to_report_when_nothing_failed(
        self, racing_engine: Engine, db_errors: DatabaseErrors
    ) -> None:
        with racing_engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        assert db_errors.seen == []


class TestRace:
    def test_releases_every_call_at_once(self) -> None:
        # Each call waits for all the others, which only works if they run at the same time.
        everyone_is_running = threading.Barrier(4)

        def call() -> Response:
            everyone_is_running.wait(timeout=5)
            return Response(204)

        responses = race([call] * 4)

        assert [response.status_code for response in responses] == [204] * 4

    def test_raises_what_a_call_raised(self) -> None:
        def failing() -> Response:
            raise ValueError("the call failed")

        with pytest.raises(ValueError, match="the call failed"):
            race([failing])
