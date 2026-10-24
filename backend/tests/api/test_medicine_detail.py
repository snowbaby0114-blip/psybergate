"""GET /api/v1/medicines/{code}: the medicine and the rule in force on today's SAST date."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.models import FormularyRule, Medicine
from tests.api.support import FakeClock, assert_problem

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]


def detail(client: TestClient, code: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/medicines/{code}")
    assert response.status_code == 200, response.text
    return response.json()


def test_returns_the_medicine_with_the_rule_in_force_today(
    client: TestClient, make_medicine: MedicineFactory, make_rule: RuleFactory
) -> None:
    medicine = make_medicine(code="MED-0001", name="Amoxil", form="capsule", strength_value=500)
    rule = make_rule(
        medicine,
        date(2026, 3, 1),
        max_quantity_per_dispense=60,
        max_quantity_per_30_days=120,
        requires_authorisation=True,
    )

    body = detail(client, "MED-0001")

    created_at = body["current_rule"].pop("created_at")
    assert datetime.fromisoformat(created_at).utcoffset().total_seconds() == 0
    assert created_at.endswith("Z")
    assert body == {
        "code": "MED-0001",
        "name": "Amoxil",
        "form": "capsule",
        "strength_value": 500,
        "strength_unit": "mg",
        "is_active": True,
        "as_of": "2026-06-15",
        "current_rule": {
            "id": rule.id,
            "effective_from": "2026-03-01",
            "effective_to": None,
            "max_quantity_per_dispense": 60,
            "max_quantity_per_30_days": 120,
            "requires_authorisation": True,
            "status": "in_force",
        },
    }


def test_current_rule_is_null_without_any_rule(
    client: TestClient, make_medicine: MedicineFactory
) -> None:
    make_medicine(code="MED-0001")

    body = detail(client, "MED-0001")

    assert body["current_rule"] is None
    assert body["as_of"] == "2026-06-15"


def test_current_rule_is_null_when_every_rule_is_expired_or_scheduled(
    client: TestClient, make_medicine: MedicineFactory, make_rule: RuleFactory
) -> None:
    medicine = make_medicine(code="MED-0001")
    make_rule(medicine, date(2026, 1, 1), date(2026, 3, 1))
    make_rule(medicine, date(2026, 9, 1))

    assert detail(client, "MED-0001")["current_rule"] is None


def test_picks_the_rule_in_force_from_a_history(
    client: TestClient, make_medicine: MedicineFactory, make_rule: RuleFactory
) -> None:
    medicine = make_medicine(code="MED-0001")
    make_rule(medicine, date(2025, 1, 1), date(2026, 1, 1))
    current = make_rule(medicine, date(2026, 1, 1), date(2026, 9, 1))
    make_rule(medicine, date(2026, 9, 1))

    assert detail(client, "MED-0001")["current_rule"]["id"] == current.id


class TestBusinessDate:
    """ "Today" is the SAST business date of the clock, never the UTC date."""

    @pytest.fixture
    def timeline(self, make_medicine: MedicineFactory, make_rule: RuleFactory) -> dict[str, Any]:
        medicine = make_medicine(code="MED-0001")
        before = make_rule(medicine, date(2026, 1, 1), date(2026, 3, 1))
        after = make_rule(medicine, date(2026, 3, 1))
        return {"before": before.id, "after": after.id}

    def test_a_rule_starting_tomorrow_in_sast_is_in_force_from_22_00_utc(
        self, client: TestClient, clock: FakeClock, timeline: dict[str, Any]
    ) -> None:
        clock.now = datetime(2026, 2, 28, 22, 30, tzinfo=UTC)  # 00:30 on 1 March in SAST

        body = detail(client, "MED-0001")

        assert body["as_of"] == "2026-03-01"
        assert body["current_rule"]["id"] == timeline["after"]

    def test_one_second_earlier_it_is_still_the_previous_rule(
        self, client: TestClient, clock: FakeClock, timeline: dict[str, Any]
    ) -> None:
        clock.now = datetime(2026, 2, 28, 21, 59, 59, tzinfo=UTC)  # 23:59:59 on 28 February

        body = detail(client, "MED-0001")

        assert body["as_of"] == "2026-02-28"
        assert body["current_rule"]["id"] == timeline["before"]

    def test_the_boundary_instant_belongs_to_the_new_day(
        self, client: TestClient, clock: FakeClock, timeline: dict[str, Any]
    ) -> None:
        clock.now = datetime(2026, 2, 28, 22, 0, tzinfo=UTC)

        assert detail(client, "MED-0001")["current_rule"]["id"] == timeline["after"]

    def test_as_of_is_the_sast_date_late_in_the_utc_day(
        self, client: TestClient, clock: FakeClock, timeline: dict[str, Any]
    ) -> None:
        clock.now = datetime(2026, 6, 15, 23, 59, tzinfo=UTC)

        assert detail(client, "MED-0001")["as_of"] == "2026-06-16"


def test_an_unknown_code_is_a_404_problem(client: TestClient) -> None:
    response = client.get("/api/v1/medicines/NOPE")

    body = assert_problem(response, 404, "medicine_not_found")
    assert body["violations"] == []
    assert "NOPE" in body["detail"]


def test_codes_are_matched_exactly(client: TestClient, make_medicine: MedicineFactory) -> None:
    make_medicine(code="MED-0001")

    assert_problem(client.get("/api/v1/medicines/med-0001"), 404, "medicine_not_found")


def test_a_code_with_spaces_and_symbols_is_addressed_percent_encoded(
    client: TestClient, make_medicine: MedicineFactory
) -> None:
    make_medicine(code="X 1+2")

    assert detail(client, "X%201%2B2")["code"] == "X 1+2"


def test_whitespace_around_the_code_in_the_path_is_ignored(
    client: TestClient, make_medicine: MedicineFactory
) -> None:
    make_medicine(code="MED-0001")

    assert detail(client, "%20MED-0001%20")["code"] == "MED-0001"


@pytest.mark.parametrize(
    ("code", "violation"),
    [("a%00b", "invalid_format"), ("x" * 65, "too_long")],
)
def test_a_malformed_code_is_a_validation_error_not_a_server_error(
    client: TestClient, code: str, violation: str
) -> None:
    response = client.get(f"/api/v1/medicines/{code}")

    body = assert_problem(response, 422, "validation_error")
    assert [(v["code"], v["field"]) for v in body["violations"]] == [(violation, "code")]
