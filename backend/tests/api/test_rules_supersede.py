"""POST /api/v1/medicines/{code}/rules: introducing a rule that supersedes what is in force."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import Engine, text
from starlette.testclient import TestClient

from app.config import Settings
from app.domain.rules import SupersedePlan
from app.models import Dispense, FormularyRule, Medicine
from app.services import formulary
from tests.api.support import FakeClock, assert_problem, violation_pairs

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]
DispenseFactory = Callable[..., Dispense]
RuleReader = Callable[[Medicine], list[FormularyRule]]

VALID_BODY = {
    "effective_from": "2026-09-01",
    "max_quantity_per_dispense": 30,
    "max_quantity_per_30_days": 60,
}


def supersede(client: TestClient, code: str = "MED-0001", **changes: Any) -> Response:
    return client.post(f"/api/v1/medicines/{code}/rules", json=VALID_BODY | changes)


def stored(rules: list[FormularyRule]) -> list[tuple[Any, ...]]:
    """Every column of every rule, so that any change to history shows up in a comparison."""
    return [
        (
            rule.id,
            rule.effective_from,
            rule.effective_to,
            rule.max_quantity_per_dispense,
            rule.max_quantity_per_30_days,
            rule.requires_authorisation,
            rule.created_at,
        )
        for rule in rules
    ]


def periods(rules: list[FormularyRule]) -> list[tuple[date, date | None]]:
    return [(rule.effective_from, rule.effective_to) for rule in rules]


@pytest.fixture
def medicine(make_medicine: MedicineFactory) -> Medicine:
    return make_medicine(code="MED-0001")


class TestPlannerBranches:
    """Each branch of the supersede planner, through the API."""

    def test_cuts_the_open_ended_rule_in_force_and_the_new_rule_is_open_ended(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        current = make_rule(medicine, date(2026, 1, 1))

        response = supersede(client, effective_from="2026-09-01")

        assert response.status_code == 201
        body = response.json()
        created = body["rule"]
        assert created.pop("created_at").endswith("Z")
        assert body["superseded"].pop("created_at").endswith("Z")
        assert body == {
            "rule": {
                "id": created["id"],
                "effective_from": "2026-09-01",
                "effective_to": None,
                "max_quantity_per_dispense": 30,
                "max_quantity_per_30_days": 60,
                "requires_authorisation": False,
                "status": "scheduled",
            },
            "superseded": {
                "id": current.id,
                "effective_from": "2026-01-01",
                "effective_to": "2026-09-01",
                "max_quantity_per_dispense": 10,
                "max_quantity_per_30_days": 30,
                "requires_authorisation": False,
                "status": "in_force",
            },
        }
        assert periods(read_rules(medicine)) == [
            (date(2026, 1, 1), date(2026, 9, 1)),
            (date(2026, 9, 1), None),
        ]

    def test_the_new_rule_inherits_the_end_of_a_closed_rule_it_cuts(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        make_rule(medicine, date(2026, 1, 1), date(2026, 12, 1))

        response = supersede(client, effective_from="2026-04-01")

        assert response.status_code == 201
        assert response.json()["rule"]["effective_to"] == "2026-12-01"
        assert response.json()["superseded"]["effective_to"] == "2026-04-01"
        assert periods(read_rules(medicine)) == [
            (date(2026, 1, 1), date(2026, 4, 1)),
            (date(2026, 4, 1), date(2026, 12, 1)),
        ]

    def test_later_scheduled_rules_are_left_untouched(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        make_rule(medicine, date(2026, 1, 1), date(2026, 6, 1))
        later = make_rule(medicine, date(2026, 6, 1), max_quantity_per_dispense=99)
        later_before = stored([later])

        response = supersede(client, effective_from="2026-03-01")

        assert response.status_code == 201
        assert periods(read_rules(medicine)) == [
            (date(2026, 1, 1), date(2026, 3, 1)),
            (date(2026, 3, 1), date(2026, 6, 1)),
            (date(2026, 6, 1), None),
        ]
        assert stored(read_rules(medicine))[-1:] == later_before

    def test_a_date_in_a_gap_cuts_nothing_and_runs_until_the_next_rule(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        make_rule(medicine, date(2026, 1, 1), date(2026, 3, 1))
        make_rule(medicine, date(2026, 6, 1))
        before = stored(read_rules(medicine))

        response = supersede(client, effective_from="2026-04-01")

        assert response.status_code == 201
        assert response.json()["superseded"] is None
        assert response.json()["rule"]["effective_to"] == "2026-06-01"
        after = stored(read_rules(medicine))
        assert [after[0], after[2]] == before
        assert periods(read_rules(medicine))[1] == (date(2026, 4, 1), date(2026, 6, 1))

    def test_a_date_before_the_first_rule_runs_until_it_starts(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        make_rule(medicine, date(2026, 3, 1))

        response = supersede(client, effective_from="2026-01-01")

        assert response.status_code == 201
        assert response.json()["superseded"] is None
        assert periods(read_rules(medicine)) == [
            (date(2026, 1, 1), date(2026, 3, 1)),
            (date(2026, 3, 1), None),
        ]

    def test_a_date_after_the_last_closed_rule_is_open_ended(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        make_rule(medicine, date(2026, 1, 1), date(2026, 3, 1))

        response = supersede(client, effective_from="2026-06-01")

        assert response.status_code == 201
        assert response.json()["superseded"] is None
        assert periods(read_rules(medicine)) == [
            (date(2026, 1, 1), date(2026, 3, 1)),
            (date(2026, 6, 1), None),
        ]

    def test_the_date_a_rule_ends_on_is_free_because_the_end_is_exclusive(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        ended = make_rule(medicine, date(2026, 1, 1), date(2026, 3, 1))
        before = stored([ended])

        response = supersede(client, effective_from="2026-03-01")

        assert response.status_code == 201
        assert response.json()["superseded"] is None
        assert stored(read_rules(medicine))[:1] == before

    def test_the_first_rule_of_a_medicine_without_any(
        self, client: TestClient, medicine: Medicine, read_rules: RuleReader
    ) -> None:
        response = supersede(client, effective_from="2026-03-01")

        assert response.status_code == 201
        assert response.json()["superseded"] is None
        assert periods(read_rules(medicine)) == [(date(2026, 3, 1), None)]

    def test_a_rule_can_be_cut_down_to_a_single_day(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        make_rule(medicine, date(2026, 1, 1))

        assert supersede(client, effective_from="2026-01-02").status_code == 201
        assert periods(read_rules(medicine))[0] == (date(2026, 1, 1), date(2026, 1, 2))

    def test_a_new_rule_can_take_the_last_day_of_the_rule_it_cuts(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        make_rule(medicine, date(2026, 1, 1), date(2026, 3, 1))

        assert supersede(client, effective_from="2026-02-28").status_code == 201
        assert periods(read_rules(medicine)) == [
            (date(2026, 1, 1), date(2026, 2, 28)),
            (date(2026, 2, 28), date(2026, 3, 1)),
        ]

    def test_a_rule_that_already_starts_on_the_date_is_a_conflict_and_nothing_changes(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        existing = make_rule(medicine, date(2026, 1, 1), date(2026, 6, 1))
        make_rule(medicine, date(2026, 6, 1))
        before = stored(read_rules(medicine))

        response = supersede(client, effective_from="2026-01-01")

        body = assert_problem(response, 409, "rule_conflict")
        assert violation_pairs(body) == [("rule_already_starts_on_date", "effective_from")]
        assert body["violations"][0]["context"] == {
            "rule_id": existing.id,
            "effective_from": "2026-01-01",
        }
        assert stored(read_rules(medicine)) == before

    def test_a_scheduled_rules_start_date_is_a_conflict_too(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
    ) -> None:
        make_rule(medicine, date(2026, 1, 1), date(2026, 9, 1))
        make_rule(medicine, date(2026, 9, 1))
        before = stored(read_rules(medicine))

        response = supersede(client, effective_from="2026-09-01")

        assert_problem(response, 409, "rule_conflict")
        assert stored(read_rules(medicine)) == before

    def test_the_limits_and_the_authorisation_flag_come_from_the_request(
        self, client: TestClient, medicine: Medicine, read_rules: RuleReader
    ) -> None:
        response = supersede(
            client,
            max_quantity_per_dispense=5,
            max_quantity_per_30_days=15,
            requires_authorisation=True,
        )

        created = response.json()["rule"]
        assert (created["max_quantity_per_dispense"], created["max_quantity_per_30_days"]) == (
            5,
            15,
        )
        assert created["requires_authorisation"] is True
        [rule] = read_rules(medicine)
        assert (rule.max_quantity_per_dispense, rule.max_quantity_per_30_days) == (5, 15)
        assert rule.requires_authorisation is True

    def test_a_rule_starting_today_is_in_force_and_the_one_it_cut_is_expired(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, clock: FakeClock
    ) -> None:
        make_rule(medicine, date(2026, 1, 1))
        clock.now = datetime(2026, 6, 14, 22, 30, tzinfo=UTC)  # 00:30 on 15 June in SAST

        body = supersede(client, effective_from="2026-06-15").json()

        assert body["rule"]["status"] == "in_force"
        assert body["superseded"]["status"] == "expired"

    def test_a_backdated_rule_is_accepted_while_no_dispense_is_affected(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory
    ) -> None:
        make_rule(medicine, date(2026, 1, 1))

        response = supersede(client, effective_from="2026-02-01")

        assert response.status_code == 201
        assert response.json()["rule"]["status"] == "in_force"

    def test_the_response_points_at_the_rule_collection(
        self, client: TestClient, make_medicine: MedicineFactory
    ) -> None:
        make_medicine(code="X 1+2")

        response = supersede(client, code="X%201%2B2")

        assert response.status_code == 201
        assert response.headers["location"] == "/api/v1/medicines/X%201%2B2/rules"
        assert client.get(response.headers["location"]).status_code == 200

    def test_the_endpoints_of_the_supported_date_range_are_accepted(
        self, client: TestClient, medicine: Medicine, read_rules: RuleReader
    ) -> None:
        assert supersede(client, effective_from="1990-01-01").status_code == 201
        assert supersede(client, effective_from="2100-12-31").status_code == 201
        assert periods(read_rules(medicine)) == [
            (date(1990, 1, 1), date(2100, 12, 31)),
            (date(2100, 12, 31), None),
        ]


class TestRecordedDispenses:
    """A rule change may not re-govern a dispense that is already on record."""

    @pytest.fixture
    def current(self, medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
        return make_rule(medicine, date(2026, 1, 1))

    def test_refuses_a_rule_that_would_govern_a_recorded_dispense(
        self,
        client: TestClient,
        medicine: Medicine,
        current: FormularyRule,
        make_dispense: DispenseFactory,
        read_rules: RuleReader,
    ) -> None:
        make_dispense(medicine, current, dispensed_at=datetime(2026, 4, 1, 8, 0, tzinfo=UTC))
        make_dispense(medicine, current, dispensed_at=datetime(2026, 5, 1, 9, 30, 15, tzinfo=UTC))
        before = stored(read_rules(medicine))

        response = supersede(client, effective_from="2026-03-01")

        body = assert_problem(response, 409, "rule_conflict")
        assert violation_pairs(body) == [("rule_would_alter_recorded_dispenses", "effective_from")]
        assert body["violations"][0]["context"] == {
            "dispense_count": 2,
            "earliest_dispensed_at": "2026-04-01T08:00:00Z",
            "latest_dispensed_at": "2026-05-01T09:30:15Z",
        }
        assert stored(read_rules(medicine)) == before

    def test_names_the_dispenses_in_business_time_not_in_utc(
        self,
        client: TestClient,
        medicine: Medicine,
        current: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        # 22:30 UTC on 1 April is 00:30 on 2 April in Johannesburg: the day differs, and the
        # message is read by people who work in SAST while the context keeps the UTC instant.
        make_dispense(medicine, current, dispensed_at=datetime(2026, 4, 1, 22, 30, tzinfo=UTC))
        make_dispense(medicine, current, dispensed_at=datetime(2026, 5, 1, 9, 30, tzinfo=UTC))

        body = assert_problem(supersede(client, effective_from="2026-03-01"), 409, "rule_conflict")

        violation = body["violations"][0]
        assert "between 2026-04-02 00:30 SAST and 2026-05-01 11:30 SAST." in violation["message"]
        assert violation["context"]["earliest_dispensed_at"] == "2026-04-01T22:30:00Z"

    def test_a_dispense_before_the_new_rule_starts_does_not_matter(
        self,
        client: TestClient,
        medicine: Medicine,
        current: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        make_dispense(medicine, current, dispensed_at=datetime(2026, 2, 1, 8, 0, tzinfo=UTC))

        assert supersede(client, effective_from="2026-03-01").status_code == 201

    @pytest.mark.parametrize(
        ("dispensed_at", "refused"),
        [
            (datetime(2026, 3, 9, 21, 59, 59, tzinfo=UTC), False),  # 23:59:59 on 9 March in SAST
            (datetime(2026, 3, 9, 22, 0, tzinfo=UTC), True),  # SAST midnight starting 10 March
        ],
    )
    def test_the_new_rule_starts_at_sast_midnight(
        self,
        client: TestClient,
        medicine: Medicine,
        current: FormularyRule,
        make_dispense: DispenseFactory,
        dispensed_at: datetime,
        refused: bool,
    ) -> None:
        make_dispense(medicine, current, dispensed_at=dispensed_at)

        response = supersede(client, effective_from="2026-03-10")

        assert response.status_code == (409 if refused else 201)

    @pytest.mark.parametrize(
        ("dispensed_at", "refused"),
        [
            (datetime(2026, 5, 31, 21, 59, 59, tzinfo=UTC), True),  # last second of 31 May
            (datetime(2026, 5, 31, 22, 0, tzinfo=UTC), False),  # 1 June: the next rule's day
        ],
    )
    def test_only_dispenses_inside_the_span_the_new_rule_would_cover_count(
        self,
        client: TestClient,
        medicine: Medicine,
        make_rule: RuleFactory,
        make_dispense: DispenseFactory,
        dispensed_at: datetime,
        refused: bool,
    ) -> None:
        cut = make_rule(medicine, date(2026, 1, 1), date(2026, 6, 1))
        later = make_rule(medicine, date(2026, 6, 1))
        make_dispense(medicine, later if not refused else cut, dispensed_at=dispensed_at)

        response = supersede(client, effective_from="2026-03-10")

        assert response.status_code == (409 if refused else 201)

    def test_dispenses_of_other_medicines_are_irrelevant(
        self,
        client: TestClient,
        medicine: Medicine,
        current: FormularyRule,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
        make_dispense: DispenseFactory,
    ) -> None:
        other = make_medicine(code="MED-0002")
        make_dispense(
            other,
            make_rule(other, date(2026, 1, 1)),
            dispensed_at=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
        )

        assert supersede(client, effective_from="2026-03-01").status_code == 201

    def test_a_dispense_in_the_future_blocks_an_open_ended_rule_from_an_earlier_date(
        self,
        client: TestClient,
        medicine: Medicine,
        current: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        make_dispense(medicine, current, dispensed_at=datetime(2026, 12, 1, 8, 0, tzinfo=UTC))

        assert_problem(supersede(client, effective_from="2026-09-01"), 409, "rule_conflict")


def test_a_chain_of_supersedes_never_loses_or_rewrites_a_rule(
    client: TestClient, medicine: Medicine, make_rule: RuleFactory, read_rules: RuleReader
) -> None:
    first_seen: dict[int, tuple[Any, ...]] = {}

    def remember() -> None:
        for row in stored(read_rules(medicine)):
            first_seen.setdefault(row[0], row)

    make_rule(medicine, date(2026, 1, 1))
    remember()
    for effective_from in ("2026-03-01", "2026-02-01", "2026-07-01"):
        assert supersede(client, effective_from=effective_from).status_code == 201
        remember()

    now = {row[0]: row for row in stored(read_rules(medicine))}
    assert set(now) == set(first_seen)
    for rule_id, row in now.items():
        # Everything except effective_to (position 2) is exactly as first stored.
        assert row[:2] + row[3:] == first_seen[rule_id][:2] + first_seen[rule_id][3:]
    assert periods(read_rules(medicine)) == [
        (date(2026, 1, 1), date(2026, 2, 1)),
        (date(2026, 2, 1), date(2026, 3, 1)),
        (date(2026, 3, 1), date(2026, 7, 1)),
        (date(2026, 7, 1), None),
    ]


INVALID_FIELDS = {
    "month-13": ({"effective_from": "2026-13-01"}, [("invalid_format", "effective_from")]),
    "no-30th-february": ({"effective_from": "2026-02-30"}, [("invalid_format", "effective_from")]),
    "wrong-date-layout": ({"effective_from": "01/09/2026"}, [("invalid_format", "effective_from")]),
    "timestamp-not-date": (
        {"effective_from": "2026-09-01T00:00:00"},
        [("invalid_format", "effective_from")],
    ),
    "leading-space": ({"effective_from": " 2026-09-01"}, [("invalid_format", "effective_from")]),
    "date-as-number": ({"effective_from": 20260901}, [("invalid_type", "effective_from")]),
    "date-null": ({"effective_from": None}, [("invalid_type", "effective_from")]),
    "date-before-1990": ({"effective_from": "1989-12-31"}, [("out_of_range", "effective_from")]),
    "date-after-2100": ({"effective_from": "2101-01-01"}, [("out_of_range", "effective_from")]),
    "per-dispense-zero": (
        {"max_quantity_per_dispense": 0},
        [("out_of_range", "max_quantity_per_dispense")],
    ),
    "per-dispense-negative": (
        {"max_quantity_per_dispense": -5},
        [("out_of_range", "max_quantity_per_dispense")],
    ),
    "per-30-days-beyond-integer-column": (
        {"max_quantity_per_30_days": 2**31},
        [("out_of_range", "max_quantity_per_30_days")],
    ),
    "per-30-days-as-text": (
        {"max_quantity_per_30_days": "60"},
        [("invalid_type", "max_quantity_per_30_days")],
    ),
    "per-30-days-fractional": (
        {"max_quantity_per_30_days": 60.5},
        [("invalid_type", "max_quantity_per_30_days")],
    ),
    "per-dispense-boolean": (
        {"max_quantity_per_dispense": True},
        [("invalid_type", "max_quantity_per_dispense")],
    ),
    "per-dispense-null": (
        {"max_quantity_per_dispense": None},
        [("invalid_type", "max_quantity_per_dispense")],
    ),
    "authorisation-as-text": (
        {"requires_authorisation": "yes"},
        [("invalid_type", "requires_authorisation")],
    ),
    "authorisation-as-number": (
        {"requires_authorisation": 1},
        [("invalid_type", "requires_authorisation")],
    ),
    "authorisation-null": (
        {"requires_authorisation": None},
        [("invalid_type", "requires_authorisation")],
    ),
    "misspelt-field": ({"effective_form": "2026-09-01"}, [("unknown_field", "effective_form")]),
}


class TestRequestValidation:
    @pytest.mark.parametrize(
        ("changes", "expected"), INVALID_FIELDS.values(), ids=INVALID_FIELDS.keys()
    )
    def test_names_the_field_and_the_cause(
        self,
        client: TestClient,
        medicine: Medicine,
        read_rules: RuleReader,
        changes: dict[str, Any],
        expected: list[tuple[str, str]],
    ) -> None:
        response = supersede(client, **changes)

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == expected
        assert read_rules(medicine) == []

    def test_reports_every_problem_at_once(self, client: TestClient, medicine: Medicine) -> None:
        response = client.post(
            "/api/v1/medicines/MED-0001/rules",
            json={
                "effective_from": "soon",
                "max_quantity_per_dispense": 0,
                "max_quantity_per_30_days": "many",
                "requires_authorisation": "maybe",
                "colour": "red",
            },
        )

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [
            ("invalid_format", "effective_from"),
            ("out_of_range", "max_quantity_per_dispense"),
            ("invalid_type", "max_quantity_per_30_days"),
            ("invalid_type", "requires_authorisation"),
            ("unknown_field", "colour"),
        ]

    def test_a_missing_field_is_required(self, client: TestClient, medicine: Medicine) -> None:
        response = client.post("/api/v1/medicines/MED-0001/rules", json={})

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [
            ("required", "effective_from"),
            ("required", "max_quantity_per_dispense"),
            ("required", "max_quantity_per_30_days"),
        ]

    def test_the_out_of_range_context_states_the_bounds(
        self, client: TestClient, medicine: Medicine
    ) -> None:
        body = supersede(client, effective_from="2101-01-01").json()
        assert body["violations"][0]["context"] == {"min": "1990-01-01", "max": "2100-12-31"}

        body = supersede(client, max_quantity_per_dispense=0).json()
        assert body["violations"][0]["context"] == {"ge": 1}

    def test_a_missing_body_is_one_required_violation_without_a_field(
        self, client: TestClient, medicine: Medicine
    ) -> None:
        body = assert_problem(
            client.post("/api/v1/medicines/MED-0001/rules"), 422, "validation_error"
        )

        assert violation_pairs(body) == [("required", None)]

    def test_malformed_json_is_reported_without_a_field(
        self, client: TestClient, medicine: Medicine
    ) -> None:
        response = client.post(
            "/api/v1/medicines/MED-0001/rules",
            content=b'{"effective_from": ',
            headers={"content-type": "application/json"},
        )

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [("invalid_json", None)]
        assert body["violations"][0]["context"] == {"position": 19}

    @pytest.mark.parametrize("payload", [b"[]", b'"text"', b"42", b"null"])
    def test_a_body_that_is_not_a_json_object_is_an_invalid_type(
        self, client: TestClient, medicine: Medicine, payload: bytes
    ) -> None:
        response = client.post(
            "/api/v1/medicines/MED-0001/rules",
            content=payload,
            headers={"content-type": "application/json"},
        )

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [("invalid_type", None)]

    def test_json_sent_with_another_content_type_is_not_read(
        self, client: TestClient, medicine: Medicine
    ) -> None:
        response = client.post(
            "/api/v1/medicines/MED-0001/rules",
            content=b'{"effective_from": "2026-09-01"}',
            headers={"content-type": "text/plain"},
        )

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [("invalid_type", None)]
        assert "application/json" in body["violations"][0]["message"]

    def test_input_values_are_never_echoed_back(
        self, client: TestClient, medicine: Medicine
    ) -> None:
        response = supersede(client, effective_from="hunter2-secret", max_quantity_per_dispense="x")

        assert response.status_code == 422
        assert "hunter2" not in response.text


def test_an_unknown_medicine_is_a_404_problem(client: TestClient) -> None:
    response = supersede(client, code="NOPE")

    body = assert_problem(response, 404, "medicine_not_found")
    assert body["violations"] == []


class TestFailureHandling:
    def test_the_exclusion_constraint_backs_up_the_planner_and_rolls_the_cut_back(
        self,
        client: TestClient,
        medicine: Medicine,
        make_rule: RuleFactory,
        read_rules: RuleReader,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = make_rule(medicine, date(2026, 1, 1), date(2026, 6, 1))
        make_rule(medicine, date(2026, 6, 1))
        before = stored(read_rules(medicine))
        # A faulty plan: it cuts the first rule but forgets that the new rule must end where the
        # next one starts, so the INSERT overlaps it.
        monkeypatch.setattr(
            formulary,
            "plan_supersede",
            lambda rules, effective_from: SupersedePlan(
                truncate_rule_id=first.id, new_effective_to=None
            ),
        )

        response = supersede(client, effective_from="2026-03-01")

        body = assert_problem(response, 409, "rule_conflict")
        assert violation_pairs(body) == [("rule_period_overlap", "effective_from")]
        assert stored(read_rules(medicine)) == before

    def test_a_supersede_waits_for_a_dispense_in_flight_and_gives_up_with_503(
        self,
        client: TestClient,
        engine: Engine,
        settings: Settings,
        medicine: Medicine,
        make_rule: RuleFactory,
        read_rules: RuleReader,
    ) -> None:
        make_rule(medicine, date(2026, 1, 1))
        before = stored(read_rules(medicine))
        settings.lock_timeout_ms = 100

        with engine.connect() as dispense_in_flight:
            # A dispense holds the medicine row FOR SHARE until it commits. That does not stop
            # the foreign-key check of an insert, so only the supersede's own FOR UPDATE waits.
            dispense_in_flight.execute(
                text("SELECT id FROM medicines WHERE id = :id FOR SHARE"), {"id": medicine.id}
            )
            response = supersede(client, effective_from="2026-03-01")

        body = assert_problem(response, 503, "resource_busy")
        assert response.headers["retry-after"] == "1"
        assert body["violations"] == []
        assert stored(read_rules(medicine)) == before
        assert supersede(client, effective_from="2026-03-01").status_code == 201
