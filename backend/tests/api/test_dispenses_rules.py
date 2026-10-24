"""POST /api/v1/dispenses: how the formulary rules are applied, through the whole stack.

The rule engine has its own tests; these show that the service hands it the right rules and the
right history, in the right time zone, and that every violation reaches the client.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app.models import Dispense, FormularyRule, Medicine
from tests.api.dispense_support import OMIT, Ledger, post_dispense
from tests.api.support import assert_problem, violation_pairs

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]
DispenseFactory = Callable[..., Dispense]

ATTEMPT_ID = frozenset({"attempt_id"})


def at(month: int, day: int, hour: int = 10, minute: int = 0, second: int = 0) -> datetime:
    """An instant in 2026, in UTC."""
    return datetime(2026, month, day, hour, minute, second, tzinfo=UTC)


def iso(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def rejected(response: Response) -> dict[str, Any]:
    return assert_problem(response, 422, "dispense_rejected", extra_keys=ATTEMPT_ID)


def codes(body: dict[str, Any]) -> list[str]:
    return [code for code, _ in violation_pairs(body)]


@pytest.fixture
def ledger(session_factory: sessionmaker[Session]) -> Ledger:
    return Ledger(session_factory)


@pytest.fixture
def medicine(make_medicine: MedicineFactory) -> Medicine:
    return make_medicine(code="MED-0001")


@pytest.fixture
def rule(medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
    """In force since 1 January 2026: at most 10 per dispense and 30 per 30 days, no
    authorisation needed."""
    return make_rule(medicine, date(2026, 1, 1))


class TestEachRule:
    def test_a_quantity_above_the_per_dispense_limit_is_rejected(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        body = rejected(post_dispense(client, quantity=11))

        assert violation_pairs(body) == [("max_quantity_per_dispense_exceeded", "quantity")]
        assert body["violations"][0]["context"] == {
            "limit": 10,
            "requested": 11,
            "rule_id": rule.id,
        }

    def test_a_quantity_equal_to_the_limit_is_accepted(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        assert post_dispense(client, quantity=10).status_code == 201

    def test_the_total_of_30_days_may_reach_its_limit_but_not_pass_it(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        make_dispense(medicine, rule, quantity=10, dispensed_at=at(3, 1))
        make_dispense(medicine, rule, quantity=10, dispensed_at=at(3, 5))

        reaching = post_dispense(client, idempotency_key="reaching", quantity=10)
        passing = post_dispense(client, idempotency_key="passing", quantity=1)

        assert reaching.status_code == 201
        body = rejected(passing)
        assert violation_pairs(body) == [("max_quantity_per_30_days_exceeded", "quantity")]
        assert body["violations"][0]["context"] == {
            "limit": 30,
            "already_dispensed": 30,
            "requested": 1,
            "window_start": "2026-02-14",
            "window_end": "2026-03-15",
            "rule_id": rule.id,
        }

    def test_only_the_same_patient_and_the_same_medicine_count_towards_the_total(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
        make_dispense: DispenseFactory,
    ) -> None:
        other = make_medicine(code="MED-0002")
        other_rule = make_rule(other, date(2026, 1, 1))
        make_dispense(medicine, rule, patient_ref="PT-2", quantity=10, dispensed_at=at(3, 10))
        make_dispense(medicine, rule, patient_ref="PT-2", quantity=10, dispensed_at=at(3, 11))
        make_dispense(other, other_rule, quantity=10, dispensed_at=at(3, 10))
        make_dispense(other, other_rule, quantity=10, dispensed_at=at(3, 11))

        response = post_dispense(client, quantity=10)

        assert response.status_code == 201

    def test_the_authorisation_reference_is_required_when_the_rule_says_so(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory
    ) -> None:
        required = make_rule(medicine, date(2026, 1, 1), requires_authorisation=True)

        missing = post_dispense(client, idempotency_key="missing")
        blank = post_dispense(client, idempotency_key="blank", authorisation_ref="   ")
        given = post_dispense(client, idempotency_key="given", authorisation_ref="AUTH-1")

        for refused in (missing, blank):
            body = rejected(refused)
            assert violation_pairs(body) == [("authorisation_required", "authorisation_ref")]
            assert body["violations"][0]["context"] == {"rule_id": required.id}
        assert given.status_code == 201

    def test_an_authorisation_reference_is_not_needed_when_the_rule_does_not_say_so(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        assert post_dispense(client, authorisation_ref=OMIT).status_code == 201

    def test_an_inactive_medicine_is_rejected(
        self, client: TestClient, make_medicine: MedicineFactory, make_rule: RuleFactory
    ) -> None:
        inactive = make_medicine(code="MED-0002", is_active=False)
        make_rule(inactive, date(2026, 1, 1))

        body = rejected(post_dispense(client, medicine_code="MED-0002"))

        assert violation_pairs(body) == [("medicine_inactive", "medicine_code")]
        assert body["violations"][0]["context"] == {}

    def test_a_date_with_no_rule_in_force_is_rejected(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        body = rejected(post_dispense(client, dispensed_at="2025-12-31T10:00:00Z"))

        assert violation_pairs(body) == [("no_rule_in_force", "dispensed_at")]
        assert body["violations"][0]["context"] == {"business_date": "2025-12-31"}

    def test_a_medicine_without_any_rule_cannot_be_dispensed(
        self, client: TestClient, medicine: Medicine
    ) -> None:
        body = rejected(post_dispense(client))

        assert violation_pairs(body) == [("no_rule_in_force", "dispensed_at")]

    def test_an_unknown_medicine_is_rejected_as_a_business_rule_not_as_a_404(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        response = post_dispense(client, medicine_code="NOPE")

        body = rejected(response)
        assert violation_pairs(body) == [("medicine_not_found", "medicine_code")]
        assert body["violations"][0]["context"] == {"medicine_code": "NOPE"}

    def test_the_medicine_code_is_case_sensitive(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        body = rejected(post_dispense(client, medicine_code="med-0001"))

        assert violation_pairs(body) == [("medicine_not_found", "medicine_code")]


class TestSeveralViolationsAtOnce:
    def test_every_violated_rule_is_reported_in_the_order_of_the_catalogue(
        self,
        client: TestClient,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
        make_dispense: DispenseFactory,
    ) -> None:
        inactive = make_medicine(code="MED-0002", is_active=False)
        strict = make_rule(
            inactive,
            date(2026, 1, 1),
            max_quantity_per_dispense=5,
            max_quantity_per_30_days=8,
            requires_authorisation=True,
        )
        make_dispense(inactive, strict, quantity=3, dispensed_at=at(3, 5))
        later = make_dispense(inactive, strict, quantity=4, dispensed_at=at(3, 20))

        body = rejected(post_dispense(client, medicine_code="MED-0002", quantity=6))

        assert violation_pairs(body) == [
            ("medicine_inactive", "medicine_code"),
            ("max_quantity_per_dispense_exceeded", "quantity"),
            ("max_quantity_per_30_days_exceeded", "quantity"),
            ("authorisation_required", "authorisation_ref"),
            ("later_dispense_limit_exceeded", "quantity"),
        ]
        by_code = {violation["code"]: violation["context"] for violation in body["violations"]}
        assert by_code["max_quantity_per_dispense_exceeded"]["limit"] == 5
        assert by_code["max_quantity_per_30_days_exceeded"]["already_dispensed"] == 3
        assert by_code["later_dispense_limit_exceeded"] == {
            "affected": [
                {
                    "business_date": "2026-03-20",
                    "limit": 8,
                    "resulting_total": 13,
                    "dispense_ids": [later.id],
                }
            ]
        }
        assert body["detail"] == "The dispense was rejected for 5 reasons."

    def test_an_inactive_medicine_and_an_excessive_quantity_are_both_reported(
        self, client: TestClient, make_medicine: MedicineFactory, make_rule: RuleFactory
    ) -> None:
        inactive = make_medicine(code="MED-0002", is_active=False)
        make_rule(inactive, date(2026, 1, 1))

        body = rejected(post_dispense(client, medicine_code="MED-0002", quantity=11))

        assert codes(body) == ["medicine_inactive", "max_quantity_per_dispense_exceeded"]

    def test_an_inactive_medicine_without_a_rule_on_the_date_reports_both(
        self, client: TestClient, make_medicine: MedicineFactory
    ) -> None:
        make_medicine(code="MED-0002", is_active=False)

        body = rejected(post_dispense(client, medicine_code="MED-0002"))

        assert codes(body) == ["medicine_inactive", "no_rule_in_force"]

    def test_the_two_quantity_limits_are_reported_together(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        body = rejected(post_dispense(client, quantity=40))

        assert codes(body) == [
            "max_quantity_per_dispense_exceeded",
            "max_quantity_per_30_days_exceeded",
        ]


class TestRulePeriods:
    @pytest.fixture
    def periods(
        self, medicine: Medicine, make_rule: RuleFactory
    ) -> tuple[FormularyRule, FormularyRule]:
        """An old, strict rule until 1 March 2026 (exclusive) and a lenient one from then on."""
        old = make_rule(
            medicine,
            date(2026, 1, 1),
            date(2026, 3, 1),
            max_quantity_per_dispense=5,
            max_quantity_per_30_days=10,
        )
        new = make_rule(
            medicine, date(2026, 3, 1), max_quantity_per_dispense=50, max_quantity_per_30_days=100
        )
        return old, new

    def test_a_backdated_dispense_is_judged_by_the_rule_of_its_own_date_not_the_rule_of_today(
        self, client: TestClient, periods: tuple[FormularyRule, FormularyRule]
    ) -> None:
        old, new = periods

        refused = rejected(
            post_dispense(
                client, idempotency_key="a", quantity=20, dispensed_at="2026-02-15T10:00:00Z"
            )
        )
        accepted = post_dispense(
            client, idempotency_key="b", quantity=5, dispensed_at="2026-02-15T10:00:00Z"
        )
        today = post_dispense(client, idempotency_key="c", quantity=20, dispensed_at=OMIT)

        assert refused["violations"][0]["context"] == {
            "limit": 5,
            "requested": 20,
            "rule_id": old.id,
        }
        assert accepted.status_code == 201
        assert accepted.json()["rule_id"] == old.id
        assert today.status_code == 201
        assert today.json()["rule_id"] == new.id

    @pytest.mark.parametrize(
        ("dispensed_at", "governing"),
        [
            (at(2, 28, 21, 59, 59), "old"),  # 23:59:59 on 28 February in Johannesburg
            (at(2, 28, 22, 0, 0), "new"),  # midnight: 1 March has begun there
            (at(2, 28, 22, 30), "new"),  # 00:30 on 1 March, although it is still 28 February in UTC
            (at(3, 1, 0, 0, 0), "new"),
            (at(2, 28, 12, 0), "old"),
        ],
    )
    def test_the_rule_is_chosen_by_the_business_date_in_johannesburg_not_by_the_utc_date(
        self,
        client: TestClient,
        periods: tuple[FormularyRule, FormularyRule],
        dispensed_at: datetime,
        governing: str,
    ) -> None:
        old, new = periods

        response = post_dispense(client, quantity=5, dispensed_at=iso(dispensed_at))

        assert response.status_code == 201
        assert response.json()["rule_id"] == (old if governing == "old" else new).id

    def test_just_before_johannesburg_midnight_the_old_limits_still_apply(
        self, client: TestClient, periods: tuple[FormularyRule, FormularyRule]
    ) -> None:
        old, _ = periods

        before = rejected(post_dispense(client, quantity=20, dispensed_at="2026-02-28T21:59:59Z"))
        after = post_dispense(
            client, idempotency_key="after", quantity=20, dispensed_at="2026-02-28T22:00:00Z"
        )

        assert before["violations"][0]["context"]["rule_id"] == old.id
        assert after.status_code == 201

    def test_the_end_of_a_rule_is_exclusive_and_a_gap_has_no_rule(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory
    ) -> None:
        make_rule(medicine, date(2026, 1, 1), date(2026, 4, 1))

        last_second = post_dispense(
            client, idempotency_key="in", dispensed_at="2026-03-31T21:59:59Z"
        )
        first_after = post_dispense(
            client, idempotency_key="out", dispensed_at="2026-03-31T22:00:00Z"
        )

        assert last_second.status_code == 201
        body = rejected(first_after)
        assert violation_pairs(body) == [("no_rule_in_force", "dispensed_at")]
        assert body["violations"][0]["context"] == {"business_date": "2026-04-01"}

    def test_the_limits_of_a_period_do_not_leak_into_the_next(
        self,
        client: TestClient,
        medicine: Medicine,
        periods: tuple[FormularyRule, FormularyRule],
        make_dispense: DispenseFactory,
    ) -> None:
        old, new = periods
        make_dispense(medicine, old, quantity=10, dispensed_at=at(2, 20))

        # 10 already dispensed would block anything under the old cap of 10 per 30 days, but the
        # new period has a cap of 100.
        response = post_dispense(client, quantity=40, dispensed_at="2026-03-05T10:00:00Z")

        assert response.status_code == 201
        assert response.json()["rule_id"] == new.id

    def test_a_rule_that_is_only_scheduled_governs_dispenses_dated_in_its_period(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory
    ) -> None:
        make_rule(medicine, date(2026, 1, 1), date(2026, 9, 1))
        scheduled = make_rule(medicine, date(2026, 9, 1), requires_authorisation=True)

        body = rejected(post_dispense(client, dispensed_at="2026-09-01T10:00:00Z"))

        assert body["violations"][0]["context"] == {"rule_id": scheduled.id}


class TestTheThirtyDayWindow:
    """The window of a dispense is the 30 business days in Johannesburg that end on its own day."""

    @pytest.fixture
    def rule(self, medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
        return make_rule(
            medicine, date(2026, 1, 1), max_quantity_per_dispense=10, max_quantity_per_30_days=10
        )

    @pytest.mark.parametrize(
        ("recorded", "requested", "counts"),
        [
            pytest.param(
                at(3, 1), at(3, 30), True, id="29 days later is the last day of the window"
            ),
            pytest.param(at(3, 1), at(3, 31), False, id="30 days later is outside it"),
            pytest.param(
                at(2, 28, 22, 0, 0), at(3, 30, 21, 59, 59), True, id="from 00:00 to 23:59:59"
            ),
            pytest.param(at(2, 28, 22, 0, 0), at(3, 30, 22, 0, 0), False, id="next midnight"),
            pytest.param(at(2, 28, 21, 59, 59), at(3, 30), False, id="the day before the window"),
            pytest.param(at(2, 28, 21, 59, 59), at(3, 29), True, id="the first day of the window"),
            pytest.param(at(3, 15, 12), at(3, 15, 8), True, id="the same day, earlier in the day"),
            pytest.param(at(3, 15, 8), at(3, 15, 12), True, id="the same day, later in the day"),
        ],
    )
    def test_a_recorded_dispense_counts_exactly_when_its_day_is_inside_the_window(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
        recorded: datetime,
        requested: datetime,
        counts: bool,
    ) -> None:
        make_dispense(medicine, rule, quantity=10, dispensed_at=recorded)

        response = post_dispense(client, quantity=1, dispensed_at=iso(requested))

        if counts:
            body = rejected(response)
            assert violation_pairs(body) == [("max_quantity_per_30_days_exceeded", "quantity")]
            assert body["violations"][0]["context"]["already_dispensed"] == 10
        else:
            assert response.status_code == 201

    def test_the_window_bounds_are_reported_as_business_dates(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        make_dispense(medicine, rule, quantity=10, dispensed_at=at(3, 15))

        body = rejected(post_dispense(client, quantity=1, dispensed_at="2026-03-14T22:30:00Z"))

        context = body["violations"][0]["context"]
        assert (context["window_start"], context["window_end"]) == ("2026-02-14", "2026-03-15")

    def test_the_total_is_not_reset_by_a_new_calendar_month(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        make_dispense(medicine, rule, quantity=10, dispensed_at=at(2, 25))

        response = post_dispense(client, quantity=1, dispensed_at="2026-03-05T10:00:00Z")

        rejected(response)


class TestBackdating:
    """A dispense counts by when it occurred, never by when it was recorded, so recording a
    dispense late cannot be used to get round a limit, and cannot break one."""

    @pytest.fixture
    def rule(self, medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
        return make_rule(
            medicine, date(2026, 1, 1), max_quantity_per_dispense=10, max_quantity_per_30_days=10
        )

    def test_a_backdated_dispense_counts_dispenses_that_were_recorded_after_its_date(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        # Dated 5 March, but recorded on 12 March: two days after the 10 March of the dispense
        # that is recorded now.
        make_dispense(medicine, rule, quantity=8, dispensed_at=at(3, 5), created_at=at(3, 12, 9))

        body = rejected(post_dispense(client, quantity=5, dispensed_at="2026-03-10T10:00:00Z"))

        assert violation_pairs(body) == [("max_quantity_per_30_days_exceeded", "quantity")]
        assert body["violations"][0]["context"]["already_dispensed"] == 8

    def test_a_dispense_dated_after_the_new_one_is_not_part_of_its_window(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        make_dispense(medicine, rule, quantity=10, dispensed_at=at(3, 20), created_at=at(3, 1, 9))

        body = rejected(post_dispense(client, quantity=10, dispensed_at="2026-03-05T10:00:00Z"))

        # Not "max_quantity_per_30_days_exceeded": 0 + 10 fits. It is the recorded dispense of
        # 20 March that cannot take 10 more.
        assert codes(body) == ["later_dispense_limit_exceeded"]

    def test_a_backdated_dispense_may_not_push_a_recorded_later_one_over_its_limit(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        later = make_dispense(medicine, rule, quantity=6, dispensed_at=at(3, 20))

        body = rejected(post_dispense(client, quantity=6, dispensed_at="2026-03-10T10:00:00Z"))

        assert violation_pairs(body) == [("later_dispense_limit_exceeded", "quantity")]
        assert body["violations"][0]["context"] == {
            "affected": [
                {
                    "business_date": "2026-03-20",
                    "limit": 10,
                    "resulting_total": 12,
                    "dispense_ids": [later.id],
                }
            ]
        }

    def test_every_later_date_that_would_be_pushed_over_is_listed_with_its_dispenses(
        self,
        client: TestClient,
        medicine: Medicine,
        make_rule: RuleFactory,
        make_dispense: DispenseFactory,
    ) -> None:
        rule = make_rule(
            medicine, date(2026, 1, 1), max_quantity_per_dispense=20, max_quantity_per_30_days=20
        )
        # Within its own window of 30 days each of them is under 20: 8, then 8 + 6 + 2 = 16.
        first = make_dispense(medicine, rule, quantity=8, dispensed_at=at(3, 10))
        second = make_dispense(medicine, rule, quantity=6, dispensed_at=at(3, 12, 8))
        third = make_dispense(medicine, rule, quantity=2, dispensed_at=at(3, 12, 11))

        body = rejected(post_dispense(client, quantity=13, dispensed_at="2026-03-01T10:00:00Z"))

        assert violation_pairs(body) == [("later_dispense_limit_exceeded", "quantity")]
        assert body["violations"][0]["context"]["affected"] == [
            {
                "business_date": "2026-03-10",
                "limit": 20,
                "resulting_total": 21,
                "dispense_ids": [first.id],
            },
            {
                "business_date": "2026-03-12",
                "limit": 20,
                "resulting_total": 29,
                "dispense_ids": [second.id, third.id],
            },
        ]

    def test_a_later_date_that_still_fits_is_not_listed(
        self,
        client: TestClient,
        medicine: Medicine,
        make_rule: RuleFactory,
        make_dispense: DispenseFactory,
    ) -> None:
        rule = make_rule(
            medicine, date(2026, 1, 1), max_quantity_per_dispense=20, max_quantity_per_30_days=20
        )
        make_dispense(medicine, rule, quantity=8, dispensed_at=at(3, 10))
        crowded = make_dispense(medicine, rule, quantity=12, dispensed_at=at(3, 12))

        body = rejected(post_dispense(client, quantity=2, dispensed_at="2026-03-01T10:00:00Z"))

        # 10 March: 8 + 2 = 10 fits. 12 March: 8 + 12 + 2 = 22 does not.
        affected = body["violations"][0]["context"]["affected"]
        assert [(item["business_date"], item["dispense_ids"]) for item in affected] == [
            ("2026-03-12", [crowded.id])
        ]

    @pytest.mark.parametrize(
        ("later", "backdated", "affected"),
        [
            pytest.param(at(3, 30), at(3, 1), True, id="29 days apart"),
            pytest.param(at(3, 31), at(3, 1), False, id="30 days apart"),
            pytest.param(at(3, 30, 21, 59, 59), at(2, 28, 22, 0, 0), True, id="to the last second"),
            pytest.param(at(3, 30, 22, 0, 0), at(2, 28, 22, 0, 0), False, id="past midnight"),
            pytest.param(at(3, 30), at(2, 28, 21, 59, 59), False, id="the day before"),
        ],
    )
    def test_a_recorded_later_dispense_is_affected_exactly_when_the_new_one_is_in_its_window(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
        later: datetime,
        backdated: datetime,
        affected: bool,
    ) -> None:
        make_dispense(medicine, rule, quantity=10, dispensed_at=later)

        response = post_dispense(client, quantity=1, dispensed_at=iso(backdated))

        if affected:
            assert codes(rejected(response)) == ["later_dispense_limit_exceeded"]
        else:
            assert response.status_code == 201

    @pytest.fixture
    def strict_then_lenient(
        self, medicine: Medicine, make_rule: RuleFactory
    ) -> tuple[FormularyRule, FormularyRule]:
        strict = make_rule(
            medicine,
            date(2026, 1, 1),
            date(2026, 3, 1),
            max_quantity_per_dispense=10,
            max_quantity_per_30_days=10,
        )
        lenient = make_rule(
            medicine, date(2026, 3, 1), max_quantity_per_dispense=100, max_quantity_per_30_days=100
        )
        return strict, lenient

    def test_a_later_dispense_is_held_to_the_cap_of_its_own_date_not_of_the_new_dispense(
        self,
        client: TestClient,
        medicine: Medicine,
        strict_then_lenient: tuple[FormularyRule, FormularyRule],
        make_dispense: DispenseFactory,
    ) -> None:
        _, lenient = strict_then_lenient
        make_dispense(medicine, lenient, quantity=50, dispensed_at=at(3, 5))

        # Judged by the strict rule the new dispense fits: 0 + 10 <= 10. The recorded 50 of
        # 5 March would break the strict cap of 10, but it has a cap of 100: 50 + 10 <= 100.
        response = post_dispense(client, quantity=10, dispensed_at="2026-02-20T10:00:00Z")

        assert response.status_code == 201

    def test_a_later_dispense_is_still_protected_by_the_cap_of_its_own_date(
        self,
        client: TestClient,
        medicine: Medicine,
        strict_then_lenient: tuple[FormularyRule, FormularyRule],
        make_dispense: DispenseFactory,
    ) -> None:
        _, lenient = strict_then_lenient
        later = make_dispense(medicine, lenient, quantity=95, dispensed_at=at(3, 5))

        body = rejected(post_dispense(client, quantity=10, dispensed_at="2026-02-20T10:00:00Z"))

        assert violation_pairs(body) == [("later_dispense_limit_exceeded", "quantity")]
        assert body["violations"][0]["context"]["affected"] == [
            {
                "business_date": "2026-03-05",
                "limit": 100,
                "resulting_total": 105,
                "dispense_ids": [later.id],
            }
        ]

    def test_dispenses_recorded_out_of_date_order_are_all_accounted_for(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        # 6 on 20 March, then 6 on 10 March (refused: the 20th would hold 12), then 4 on
        # 10 March, then 1 on 25 March (refused: its window already holds 10).
        steps = [
            ("a", 6, "2026-03-20T10:00:00Z", 201),
            ("b", 6, "2026-03-10T10:00:00Z", 422),
            ("c", 4, "2026-03-10T10:00:00Z", 201),
            ("d", 1, "2026-03-25T10:00:00Z", 422),
        ]

        outcomes = [
            post_dispense(
                client, idempotency_key=key, quantity=quantity, dispensed_at=dispensed_at
            ).status_code
            for key, quantity, dispensed_at, _ in steps
        ]

        assert outcomes == [status for *_, status in steps]
        assert sorted(dispense.quantity for dispense in ledger.dispenses()) == [4, 6]
