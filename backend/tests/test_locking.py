"""lock_idempotency_key and lock_patient_medicine against a real PostgreSQL: which lock each one
takes, whom it excludes and until when. That the dispense transaction takes them in the right
order is covered with the dispense service."""

from collections.abc import Callable
from dataclasses import dataclass

import psycopg.errors
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import locking
from app.locking import advisory_key


@dataclass(frozen=True)
class LockCase:
    take: Callable[[Session], None]
    key: int
    unrelated_keys: tuple[int, ...]


CASES = {
    "idempotency key": LockCase(
        take=lambda session: locking.lock_idempotency_key(session, "key-1"),
        key=advisory_key("idem", "key-1"),
        unrelated_keys=(advisory_key("idem", "key-2"),),
    ),
    "patient and medicine": LockCase(
        take=lambda session: locking.lock_patient_medicine(session, 1, "PT-1"),
        key=advisory_key("pair", 1, "PT-1"),
        unrelated_keys=(advisory_key("pair", 2, "PT-1"), advisory_key("pair", 1, "PT-2")),
    ),
}


def is_free(engine: Engine, key: int) -> bool:
    """Whether another connection could take the advisory lock right now. If it could, it holds
    it only until the probe's connection is returned."""
    with engine.connect() as probe:
        return probe.execute(
            text("SELECT pg_try_advisory_xact_lock(CAST(:key AS bigint))"), {"key": key}
        ).scalar_one()


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
class TestLock:
    def test_is_held_until_the_transaction_commits(self, engine: Engine, case: LockCase) -> None:
        with Session(engine) as session, session.begin():
            case.take(session)

            assert not is_free(engine, case.key)
            assert all(is_free(engine, other) for other in case.unrelated_keys)

        assert is_free(engine, case.key)

    def test_is_released_when_the_transaction_rolls_back(
        self, engine: Engine, case: LockCase
    ) -> None:
        with pytest.raises(RuntimeError), Session(engine) as session, session.begin():
            case.take(session)
            raise RuntimeError("the request failed")

        assert is_free(engine, case.key)

    def test_makes_a_second_transaction_wait(self, engine: Engine, case: LockCase) -> None:
        with Session(engine) as holder, holder.begin():
            case.take(holder)

            with (
                pytest.raises(OperationalError) as refused,
                Session(engine) as waiter,
                waiter.begin(),
            ):
                waiter.execute(text("SET LOCAL lock_timeout = '100ms'"))
                case.take(waiter)

        assert isinstance(refused.value.orig, psycopg.errors.LockNotAvailable)
