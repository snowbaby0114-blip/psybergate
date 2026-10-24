"""Threads, ordering and parking: what the concurrency tests choreograph a race with, and the
assertions that the races over one patient's quota share."""

import threading
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from types import ModuleType
from typing import Any

import psycopg
import pytest
from httpx import Client, Response
from sqlalchemy import Engine, event
from sqlalchemy.engine import ExceptionContext

from app.models import FormularyRule, Medicine
from tests.api.dispense_support import Ledger, payload

# Bounds every wait of a test on another thread, so that a bug shows as a failure with a message
# instead of a hung run. Far longer than anything a healthy test waits for.
WAIT_SECONDS = 30.0


def send(client: Client, **changes: Any) -> Response:
    """POST /api/v1/dispenses with the default payload of the API tests, changed as given."""
    return client.post("/api/v1/dispenses", json=payload(**changes))


def send_rule(
    client: Client,
    code: str,
    effective_from: date,
    *,
    max_quantity_per_dispense: int = 50,
    max_quantity_per_30_days: int = 100,
) -> Response:
    return client.post(
        f"/api/v1/medicines/{code}/rules",
        json={
            "effective_from": effective_from.isoformat(),
            "max_quantity_per_dispense": max_quantity_per_dispense,
            "max_quantity_per_30_days": max_quantity_per_30_days,
        },
    )


# One thread per request in flight; the largest race has 24.
_threads = ThreadPoolExecutor(max_workers=64, thread_name_prefix="racer")
_in_flight: list[Future[Response]] = []


def start(call: Callable[[], Response]) -> Future[Response]:
    """Run the call on a thread of its own, without waiting for it."""
    request = _threads.submit(call)
    _in_flight.append(request)
    return request


def finish_requests_in_flight() -> None:
    """Wait for every request started so far. A test that failed while a request was still held
    must not leave it running into the next test, whose first statement empties the tables."""
    wait(_in_flight, timeout=WAIT_SECONDS)
    _in_flight.clear()


def answer(request: Future[Response]) -> Response:
    """The response of a request started with `start`, which a bug may keep from ever coming."""
    try:
        return request.result(WAIT_SECONDS)
    except TimeoutError:
        raise AssertionError(f"no response after {WAIT_SECONDS:g} s") from None


def race(calls: Sequence[Callable[[], Response]]) -> list[Response]:
    """Run every call on a thread of its own, all released at the same instant by a barrier,
    and return the responses in the order of the calls."""
    starting_line = threading.Barrier(len(calls))

    def at_the_line(call: Callable[[], Response]) -> Callable[[], Response]:
        def run() -> Response:
            starting_line.wait(WAIT_SECONDS)
            return call()

        return run

    return [answer(request) for request in [start(at_the_line(call)) for call in calls]]


class EventLog:
    """What happened, in the order it happened, recorded by any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[str] = []

    def record(self, event_description: str) -> None:
        with self._lock:
            self._events.append(event_description)

    @property
    def events(self) -> list[str]:
        with self._lock:
            return list(self._events)


class Gate:
    """Holds the first call of a function of the service inside its transaction, after the
    function has done its work and before it returns, until the test lets it go. Calls after the
    first pass straight through.

    The held request has taken every lock the function runs under and has not committed, which is
    the moment a second request has to be unable to overtake it. If `describe` is given, what each
    call returned is written to `log` as "A <description>" for the held call and "B <description>"
    for any later one.
    """

    def __init__(
        self,
        log: EventLog,
        describe: Callable[[Any], str] | None = None,
        only_if: Callable[..., bool] = lambda *args, **kwargs: True,
    ) -> None:
        self._log = log
        self._describe = describe
        self._only_if = only_if
        self._claim = threading.Lock()
        self._claimed = False
        self._inside = threading.Event()
        self._released = threading.Event()

    def install(self, monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str) -> None:
        function = getattr(module, name)

        def held(*args: Any, **kwargs: Any) -> Any:
            result = function(*args, **kwargs)
            with self._claim:
                first = not self._claimed and self._only_if(*args, **kwargs)
                self._claimed = self._claimed or first
            if self._describe is not None:
                self._log.record(f"{'A' if first else 'B'} {self._describe(result)}")
            if first:
                self._inside.set()
                if not self._released.wait(WAIT_SECONDS):
                    raise AssertionError("the held request was never released")
            return result

        monkeypatch.setattr(module, name, held)

    def wait_until_inside(self) -> None:
        if not self._inside.wait(WAIT_SECONDS):
            raise AssertionError("no request reached the function that holds the first one")

    def release(self) -> None:
        self._released.set()


@dataclass(frozen=True)
class DatabaseError:
    sqlstate: str | None
    constraint: str | None


class DatabaseErrors:
    """Every error PostgreSQL raised on the engine, as (SQLSTATE, constraint). A race that the
    locks decide leaves none: a request that a constraint or the deadlock detector had to stop
    was not decided by the locks."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        self._seen: list[DatabaseError] = []
        event.listen(engine, "handle_error", self._record)

    def _record(self, context: ExceptionContext) -> None:
        original = context.original_exception
        if isinstance(original, psycopg.Error):
            error = DatabaseError(original.sqlstate, original.diag.constraint_name)
            with self._lock:
                self._seen.append(error)

    @property
    def seen(self) -> list[DatabaseError]:
        with self._lock:
            return list(self._seen)

    def stop(self) -> None:
        event.remove(self._engine, "handle_error", self._record)


MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]


@dataclass
class LiveServer:
    """The application served by uvicorn on a port of its own."""

    url: str
    _clients: list[Client] = field(default_factory=list)

    def new_client(self) -> Client:
        self._clients.append(Client(base_url=self.url, timeout=WAIT_SECONDS))
        return self._clients[-1]

    def close_clients(self) -> None:
        for client in self._clients:
            client.close()


# The arithmetic of the quota races: the rule allows 100 per 30 days, each request asks for 30, so
# three fit whatever the order.
CAP = 100
QUANTITY = 30
FITTING = CAP // QUANTITY
QUOTA_EXCEEDED = "max_quantity_per_30_days_exceeded"

Pair = tuple[Medicine, str]


def requests_for(
    client_for: Callable[[], Client], pair: Pair, count: int, *, quantity: int = QUANTITY
) -> list[Callable[[], Response]]:
    medicine, patient = pair
    return [
        partial(
            send,
            client_for(),
            idempotency_key=f"{medicine.code}-{patient}-{number}",
            medicine_code=medicine.code,
            patient_ref=patient,
            quantity=quantity,
        )
        for number in range(count)
    ]


def violation_codes(response: Response) -> list[str]:
    return [violation["code"] for violation in response.json()["violations"]]


def assert_the_quota_held(
    responses: Sequence[Response],
    ledger: Ledger,
    pair: Pair,
    *,
    fitting: int = FITTING,
    quantity: int = QUANTITY,
) -> None:
    """Of these responses, for the one pair, exactly `fitting` were accepted and the others
    rejected for the quota, and the ledger holds exactly what was accepted."""
    medicine, patient = pair
    statuses = Counter(response.status_code for response in responses)
    assert statuses == {201: fitting, 422: len(responses) - fitting}
    for rejected in (response for response in responses if response.status_code == 422):
        assert violation_codes(rejected) == [QUOTA_EXCEEDED]
    dispenses = [
        dispense
        for dispense in ledger.dispenses()
        if (dispense.medicine_id, dispense.patient_ref) == (medicine.id, patient)
    ]
    attempts = [
        attempt
        for attempt in ledger.attempts()
        if (attempt.medicine_id, attempt.patient_ref) == (medicine.id, patient)
    ]
    assert len(dispenses) == fitting
    assert sum(dispense.quantity for dispense in dispenses) == fitting * quantity
    assert Counter(attempt.outcome for attempt in attempts) == {
        "accepted": fitting,
        "rejected": len(responses) - fitting,
    }
