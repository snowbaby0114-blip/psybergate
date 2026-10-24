"""Differential test: random sequences of dispenses go through the API and through a model that
is written here from the brief's rules, with plain date arithmetic and none of the engine's code.
They must agree on every request, down to each violation and its context, and every dispense the
API recorded must satisfy the invariant at the end, whatever order the requests came in.

The model deliberately works the other way round from the engine where it can. It converts
instants with a fixed +02:00 offset (Johannesburg has no daylight saving) instead of a zone, and
it decides acceptance a second time by recording the dispense tentatively and re-checking the
cap of every dispense on record, which does not need the idea of "later dates" at all.
"""

import random
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app.models import FormularyRule, Medicine
from tests.api.dispense_support import REPLAYED, Ledger

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]

SEEDS = range(8)
REQUESTS_PER_SEED = 40
FIRST_DAY = date(2026, 2, 25)  # the rules start on 1 March, so the first days have none
DAYS = 70
# Instants chosen around the edges that matter: the start and end of a Johannesburg day are at
# 22:00 and 21:59:59 UTC, so each of these falls on a different side of one.
TIMES_OF_DAY = [
    timedelta(hours=0),
    timedelta(hours=10),
    timedelta(hours=21, minutes=59, seconds=59),
    timedelta(hours=22),
    timedelta(hours=23, minutes=30),
]


@dataclass(frozen=True)
class Period:
    rule_id: int
    start: date
    end: date | None
    max_per_dispense: int
    max_per_30_days: int
    requires_authorisation: bool


@dataclass(frozen=True)
class Recorded:
    day: date
    quantity: int
    dispense_id: int


@dataclass(frozen=True)
class Submission:
    key: str
    medicine: str
    patient: str
    quantity: int
    instant: datetime
    authorisation: str | None

    def body(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.key,
            "medicine_code": self.medicine,
            "patient_ref": self.patient,
            "quantity": self.quantity,
            "dispensed_at": self.instant.isoformat().replace("+00:00", "Z"),
            "authorisation_ref": self.authorisation,
        }


def business_day(instant: datetime) -> date:
    return (instant + timedelta(hours=2)).date()


class BruteForce:
    def __init__(self, periods: Mapping[str, Sequence[Period]]) -> None:
        self.periods = periods
        self.recorded: dict[tuple[str, str], list[Recorded]] = defaultdict(list)

    def rule_on(self, medicine: str, day: date) -> Period | None:
        for period in self.periods[medicine]:
            if period.start <= day and (period.end is None or day < period.end):
                return period
        return None

    def total(self, pair: tuple[str, str], day: date, extra: Sequence[Recorded] = ()) -> int:
        """What the patient has had in the 30 days that end on `day`, including `day`."""
        return sum(
            r.quantity for r in [*self.recorded[pair], *extra] if 0 <= (day - r.day).days <= 29
        )

    def violations(self, request: Submission) -> list[tuple[str, dict[str, Any]]]:
        """The violations of the request in the order of the catalogue, with their context."""
        pair = (request.medicine, request.patient)
        day = business_day(request.instant)
        rule = self.rule_on(request.medicine, day)
        if rule is None:
            return [("no_rule_in_force", {"business_date": day.isoformat()})]

        found: list[tuple[str, dict[str, Any]]] = []
        if request.quantity > rule.max_per_dispense:
            found.append(
                (
                    "max_quantity_per_dispense_exceeded",
                    {
                        "limit": rule.max_per_dispense,
                        "requested": request.quantity,
                        "rule_id": rule.rule_id,
                    },
                )
            )
        already = self.total(pair, day)
        if already + request.quantity > rule.max_per_30_days:
            found.append(
                (
                    "max_quantity_per_30_days_exceeded",
                    {
                        "limit": rule.max_per_30_days,
                        "already_dispensed": already,
                        "requested": request.quantity,
                        "window_start": (day - timedelta(days=29)).isoformat(),
                        "window_end": day.isoformat(),
                        "rule_id": rule.rule_id,
                    },
                )
            )
        if rule.requires_authorisation and not (request.authorisation or "").strip():
            found.append(("authorisation_required", {"rule_id": rule.rule_id}))

        affected = []
        for later in sorted({r.day for r in self.recorded[pair] if 0 < (r.day - day).days <= 29}):
            later_rule = self.rule_on(request.medicine, later)
            resulting = self.total(pair, later) + request.quantity
            if later_rule is not None and resulting > later_rule.max_per_30_days:
                affected.append(
                    {
                        "business_date": later.isoformat(),
                        "limit": later_rule.max_per_30_days,
                        "resulting_total": resulting,
                        "dispense_ids": sorted(
                            r.dispense_id for r in self.recorded[pair] if r.day == later
                        ),
                    }
                )
        if affected:
            found.append(("later_dispense_limit_exceeded", {"affected": affected}))
        return found

    def accepted_by_the_invariant(self, request: Submission) -> bool:
        """The same decision made another way: record the dispense, then check that every
        dispense on record, the new one included, is within the cap of its own day."""
        pair = (request.medicine, request.patient)
        day = business_day(request.instant)
        rule = self.rule_on(request.medicine, day)
        if rule is None or request.quantity > rule.max_per_dispense:
            return False
        if rule.requires_authorisation and not (request.authorisation or "").strip():
            return False
        tentative = Recorded(day, request.quantity, dispense_id=-1)
        for dispense in [*self.recorded[pair], tentative]:
            cap = self.rule_on(request.medicine, dispense.day)
            if (
                cap is not None
                and self.total(pair, dispense.day, [tentative]) > cap.max_per_30_days
            ):
                return False
        return True

    def record(self, request: Submission, dispense_id: int) -> None:
        pair = (request.medicine, request.patient)
        self.recorded[pair].append(
            Recorded(business_day(request.instant), request.quantity, dispense_id)
        )


def random_requests(seed: int) -> list[Submission]:
    """Mostly uniform days, but often a day close to one that the same patient and medicine
    already had, shifted by 0, 1, 29 or 30 days: the edges of the window are where a rule
    about 30 days goes wrong, and uniform days would hardly ever meet them."""
    rng = random.Random(seed)
    earlier: dict[tuple[str, str], list[date]] = defaultdict(list)
    requests = []
    for number in range(REQUESTS_PER_SEED):
        medicine = rng.choice(["MED-0001", "MED-0002"])
        patient = rng.choice(["PT-A", "PT-B"])
        day = FIRST_DAY + timedelta(days=rng.randrange(DAYS))
        if earlier[medicine, patient] and rng.random() < 0.6:
            near = rng.choice(earlier[medicine, patient]) + timedelta(
                days=rng.choice([-30, -29, -1, 0, 1, 29, 30])
            )
            if 0 <= (near - FIRST_DAY).days < DAYS:
                day = near
        earlier[medicine, patient].append(day)
        midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
        requests.append(
            Submission(
                key=f"seed-{seed}-{number}",
                medicine=medicine,
                patient=patient,
                quantity=rng.randint(1, 9),
                instant=midnight + rng.choice(TIMES_OF_DAY),
                authorisation=rng.choice(["AUTH-1", "AUTH-1", "AUTH-1", "AUTH-1", None, "   "]),
            )
        )
    return requests


@pytest.fixture
def periods(
    make_medicine: MedicineFactory, make_rule: RuleFactory
) -> Mapping[str, Sequence[Period]]:
    """Two medicines with two rule periods each, with different limits and authorisation."""
    first = make_medicine(code="MED-0001")
    second = make_medicine(code="MED-0002", name="Panado")
    specs = {
        "MED-0001": (
            first,
            [
                (date(2026, 3, 1), date(2026, 4, 5), 8, 20, False),
                (date(2026, 4, 5), None, 6, 12, False),
            ],
        ),
        "MED-0002": (
            second,
            [
                (date(2026, 3, 1), date(2026, 4, 10), 9, 25, True),
                (date(2026, 4, 10), None, 5, 15, False),
            ],
        ),
    }
    return {
        code: [
            Period(
                make_rule(
                    medicine,
                    start,
                    end,
                    max_quantity_per_dispense=per_dispense,
                    max_quantity_per_30_days=per_30_days,
                    requires_authorisation=authorisation,
                ).id,
                start,
                end,
                per_dispense,
                per_30_days,
                authorisation,
            )
            for start, end, per_dispense, per_30_days, authorisation in rows
        ]
        for code, (medicine, rows) in specs.items()
    }


def without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in body.items() if name != "request_id"}


@pytest.mark.parametrize("seed", SEEDS)
def test_the_api_agrees_with_the_brute_force_model_on_every_request(
    seed: int,
    client: TestClient,
    periods: Mapping[str, Sequence[Period]],
    session_factory: sessionmaker[Session],
) -> None:
    model = BruteForce(periods)
    requests = random_requests(seed)
    answers: list[Response] = []

    for request in requests:
        expected = model.violations(request)
        assert (not expected) == model.accepted_by_the_invariant(request), request

        response = client.post("/api/v1/dispenses", json=request.body())

        if expected:
            assert response.status_code == 422, (request, response.text)
            body = response.json()
            assert body["code"] == "dispense_rejected"
            assert [(v["code"], v["context"]) for v in body["violations"]] == expected, request
        else:
            assert response.status_code == 201, (request, response.text)
            body = response.json()
            rule = model.rule_on(request.medicine, business_day(request.instant))
            assert rule is not None
            assert body["rule_id"] == rule.rule_id
            assert (body["quantity"], body["patient_ref"], body["medicine_code"]) == (
                request.quantity,
                request.patient,
                request.medicine,
            )
            model.record(request, body["id"])
        answers.append(response)

    accepted = sum(response.status_code == 201 for response in answers)
    assert 0 < accepted < len(requests), "the sequence should both accept and reject"
    check_the_database(model, requests, accepted, Ledger(session_factory))
    check_the_replays(client, requests, answers, Ledger(session_factory))


def check_the_database(
    model: BruteForce, requests: Sequence[Submission], accepted: int, ledger: Ledger
) -> None:
    dispenses, attempts = ledger.dispenses(), ledger.attempts()
    assert len(dispenses) == accepted
    assert len(attempts) == len(requests)
    assert {a.dispense_id for a in attempts if a.outcome == "accepted"} == {d.id for d in dispenses}
    assert all(a.dispense_id is None for a in attempts if a.outcome == "rejected")

    # The invariant of the brief, for every dispense that was recorded: it is within the per-dispense
    # limit of the rule in force on its day, and its 30 days are within that rule's cap.
    medicine_code = {a.medicine_id: a.medicine_code for a in attempts}
    for dispense in dispenses:
        medicine = medicine_code[dispense.medicine_id]
        day = business_day(dispense.dispensed_at)
        rule = model.rule_on(medicine, day)
        assert rule is not None and rule.rule_id == dispense.rule_id
        assert dispense.quantity <= rule.max_per_dispense
        assert model.total((medicine, dispense.patient_ref), day) <= rule.max_per_30_days


def check_the_replays(
    client: TestClient, requests: Sequence[Submission], answers: Sequence[Response], ledger: Ledger
) -> None:
    """After all that has happened since, every request is answered with its original outcome."""
    before = len(ledger.dispenses()), len(ledger.attempts())

    for request, original in zip(requests, answers, strict=True):
        replay = client.post("/api/v1/dispenses", json=request.body())

        assert replay.status_code == original.status_code, request
        assert replay.headers[REPLAYED] == "true"
        assert without_request_id(replay.json()) == without_request_id(original.json())

    assert (len(ledger.dispenses()), len(ledger.attempts())) == before
