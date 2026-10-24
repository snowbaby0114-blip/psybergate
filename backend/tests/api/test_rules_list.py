"""GET /api/v1/medicines/{code}/rules: the full rule history, ascending by effective_from."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.models import FormularyRule, Medicine
from tests.api.support import FakeClock, assert_problem

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]


def rules(client: TestClient, code: str = "MED-0001") -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/medicines/{code}/rules")
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == ["items"]
    return body["items"]


def test_lists_the_rules_with_their_documented_shape(
    client: TestClient, make_medicine: MedicineFactory, make_rule: RuleFactory
) -> None:
    medicine = make_medicine(code="MED-0001")
    rule = make_rule(
        medicine,
        date(2026, 3, 1),
        max_quantity_per_dispense=60,
        max_quantity_per_30_days=120,
        requires_authorisation=True,
    )

    [item] = rules(client)

    created_at = item.pop("created_at")
    assert created_at.endswith("Z")
    assert datetime.fromisoformat(created_at).utcoffset().total_seconds() == 0
    assert item == {
        "id": rule.id,
        "effective_from": "2026-03-01",
        "effective_to": None,
        "max_quantity_per_dispense": 60,
        "max_quantity_per_30_days": 120,
        "requires_authorisation": True,
        "status": "in_force",
    }


def test_a_medicine_without_rules_has_an_empty_history(
    client: TestClient, make_medicine: MedicineFactory
) -> None:
    make_medicine(code="MED-0001")

    assert rules(client) == []


def test_orders_ascending_by_effective_from_whatever_the_insertion_order(
    client: TestClient, make_medicine: MedicineFactory, make_rule: RuleFactory
) -> None:
    medicine = make_medicine(code="MED-0001")
    third = make_rule(medicine, date(2026, 9, 1))
    first = make_rule(medicine, date(2025, 1, 1), date(2026, 1, 1))
    second = make_rule(medicine, date(2026, 1, 1), date(2026, 9, 1))

    assert [item["id"] for item in rules(client)] == [first.id, second.id, third.id]


def test_only_the_requested_medicines_rules_are_listed(
    client: TestClient, make_medicine: MedicineFactory, make_rule: RuleFactory
) -> None:
    mine = make_rule(make_medicine(code="MED-0001"), date(2026, 1, 1))
    make_rule(make_medicine(code="MED-0002"), date(2026, 1, 1))

    assert [item["id"] for item in rules(client, "MED-0001")] == [mine.id]


def test_status_tells_expired_from_in_force_from_scheduled(
    client: TestClient, make_medicine: MedicineFactory, make_rule: RuleFactory
) -> None:
    medicine = make_medicine(code="MED-0001")
    make_rule(medicine, date(2025, 1, 1), date(2026, 1, 1))
    make_rule(medicine, date(2026, 1, 1), date(2026, 9, 1))
    make_rule(medicine, date(2026, 9, 1))

    assert [item["status"] for item in rules(client)] == ["expired", "in_force", "scheduled"]


class TestStatusBoundaries:
    """A rule applies from effective_from and stops at effective_to, which is exclusive."""

    @pytest.fixture(autouse=True)
    def _rule(self, make_medicine: MedicineFactory, make_rule: RuleFactory) -> None:
        make_rule(make_medicine(code="MED-0001"), date(2026, 3, 1), date(2026, 4, 1))

    @pytest.mark.parametrize(
        ("now", "status"),
        [
            (datetime(2026, 2, 28, 21, 59, 59, tzinfo=UTC), "scheduled"),  # 23:59:59 on 28 Feb
            (datetime(2026, 2, 28, 22, 0, tzinfo=UTC), "in_force"),  # SAST midnight, 1 March
            (datetime(2026, 3, 31, 21, 59, 59, tzinfo=UTC), "in_force"),  # last second of 31 March
            (datetime(2026, 3, 31, 22, 0, tzinfo=UTC), "expired"),  # SAST midnight, 1 April
        ],
    )
    def test_follows_the_sast_calendar_day(
        self, client: TestClient, clock: FakeClock, now: datetime, status: str
    ) -> None:
        clock.now = now

        assert rules(client)[0]["status"] == status


def test_an_unknown_code_is_a_404_problem(client: TestClient) -> None:
    body = assert_problem(client.get("/api/v1/medicines/NOPE/rules"), 404, "medicine_not_found")

    assert body["violations"] == []
