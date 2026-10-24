"""Helpers shared by the dispense API test modules."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final, Literal

from httpx import Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app.locking import advisory_key
from app.models import Dispense, DispenseAttempt

DISPENSES = "/api/v1/dispenses"
REPLAYED = "idempotency-replayed"

# Leave a field out of a request: None would send a JSON null, which is something else.
OMIT: Final = object()

PAYLOAD: Final[dict[str, Any]] = {
    "idempotency_key": "key-1",
    "medicine_code": "MED-0001",
    "patient_ref": "PT-1",
    "quantity": 5,
    "dispensed_at": "2026-03-15T10:00:00Z",
}


def payload(**changes: Any) -> dict[str, Any]:
    merged = PAYLOAD | changes
    return {name: value for name, value in merged.items() if value is not OMIT}


def post_dispense(client: TestClient, **changes: Any) -> Response:
    return client.post(DISPENSES, json=payload(**changes))


class Ledger:
    """What the database holds, read through a session of its own each time."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def dispenses(self) -> list[Dispense]:
        with self._session_factory() as session:
            return list(session.scalars(select(Dispense).order_by(Dispense.id)))

    def attempts(self) -> list[DispenseAttempt]:
        with self._session_factory() as session:
            return list(session.scalars(select(DispenseAttempt).order_by(DispenseAttempt.id)))


@contextmanager
def hold_advisory_lock(
    session_factory: sessionmaker[Session], namespace: str, *parts: str | int
) -> Iterator[None]:
    """Hold the advisory lock `advisory_key(namespace, *parts)` in an open transaction, as a
    request in flight does, so that the request under test has to wait for it."""
    with session_factory() as holder, holder.begin():
        holder.execute(
            text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"),
            {"key": advisory_key(namespace, *parts)},
        )
        yield


@contextmanager
def hold_medicine_row(
    session_factory: sessionmaker[Session], medicine_id: int, mode: Literal["SHARE", "UPDATE"]
) -> Iterator[None]:
    """Hold the medicine row in an open transaction: FOR SHARE as a dispense in flight does, FOR
    UPDATE as a change of the medicine's rules does."""
    with session_factory() as holder, holder.begin():
        holder.execute(
            text(f"SELECT id FROM medicines WHERE id = :id FOR {mode}"), {"id": medicine_id}
        )
        yield
