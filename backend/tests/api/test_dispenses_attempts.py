"""The attempt log: every well-formed request that reaches the rules leaves exactly one row, and a
rejection leaves no dispense behind."""

from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app.domain.idempotency import request_fingerprint
from app.models import Dispense, FormularyRule, Medicine
from tests.api.dispense_support import OMIT, Ledger, post_dispense
from tests.api.support import FakeClock, assert_problem, violation_pairs

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]
DispenseFactory = Callable[..., Dispense]

MARCH_15 = datetime(2026, 3, 15, 10, 0, tzinfo=UTC)


@pytest.fixture
def ledger(session_factory: sessionmaker[Session]) -> Ledger:
    return Ledger(session_factory)


@pytest.fixture
def medicine(make_medicine: MedicineFactory) -> Medicine:
    return make_medicine(code="MED-0001")


@pytest.fixture
def rule(medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
    """At most 10 per dispense and 30 per 30 days, in force since 1 January 2026."""
    return make_rule(medicine, date(2026, 1, 1))


class TestAcceptedRequest:
    def test_leaves_one_dispense_and_one_accepted_attempt_that_point_at_each_other(
        self, client: TestClient, medicine: Medicine, rule: FormularyRule, ledger: Ledger
    ) -> None:
        response = post_dispense(client, authorisation_ref="AUTH-1")

        [dispense] = ledger.dispenses()
        [attempt] = ledger.attempts()
        assert response.status_code == 201
        assert (
            attempt.idempotency_key,
            attempt.outcome,
            attempt.dispense_id,
            attempt.violations,
            attempt.medicine_code,
            attempt.medicine_id,
            attempt.patient_ref,
            attempt.quantity,
            attempt.dispensed_at,
            attempt.authorisation_ref,
        ) == (
            "key-1",
            "accepted",
            dispense.id,
            [],
            "MED-0001",
            medicine.id,
            "PT-1",
            5,
            MARCH_15,
            "AUTH-1",
        )

    def test_the_dispense_and_its_attempt_are_written_in_one_transaction(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        post_dispense(client)

        # now() is the start of the transaction, so one transaction gives both rows one instant.
        assert ledger.dispenses()[0].created_at == ledger.attempts()[0].created_at

    def test_the_attempt_carries_the_fingerprint_of_the_request(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        post_dispense(client, authorisation_ref="AUTH-1")

        assert ledger.attempts()[0].request_hash == request_fingerprint(
            medicine_code="MED-0001",
            patient_ref="PT-1",
            quantity=5,
            dispensed_at=MARCH_15,
            authorisation_ref="AUTH-1",
        )

    def test_the_dispense_records_the_rule_that_was_in_force_on_its_date(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, ledger: Ledger
    ) -> None:
        old = make_rule(medicine, date(2026, 1, 1), date(2026, 3, 1))
        new = make_rule(medicine, date(2026, 3, 1))

        post_dispense(client, idempotency_key="in-february", dispensed_at="2026-02-10T10:00:00Z")
        post_dispense(client, idempotency_key="in-march", dispensed_at="2026-03-10T10:00:00Z")

        assert [dispense.rule_id for dispense in ledger.dispenses()] == [old.id, new.id]

    def test_without_a_dispensed_at_the_attempt_logs_the_instant_that_was_used(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger, clock: FakeClock
    ) -> None:
        post_dispense(client, dispensed_at=OMIT)

        [attempt] = ledger.attempts()
        assert attempt.dispensed_at == clock.now
        assert attempt.request_hash == request_fingerprint(
            medicine_code="MED-0001",
            patient_ref="PT-1",
            quantity=5,
            dispensed_at=None,
            authorisation_ref=None,
        )


class TestRejectedRequest:
    def test_leaves_no_dispense_but_exactly_one_attempt_with_every_reason(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory, ledger: Ledger
    ) -> None:
        make_rule(
            medicine,
            date(2026, 1, 1),
            max_quantity_per_dispense=5,
            max_quantity_per_30_days=8,
            requires_authorisation=True,
        )

        response = post_dispense(client, quantity=9)

        body = assert_problem(
            response, 422, "dispense_rejected", extra_keys=frozenset({"attempt_id"})
        )
        assert violation_pairs(body) == [
            ("max_quantity_per_dispense_exceeded", "quantity"),
            ("max_quantity_per_30_days_exceeded", "quantity"),
            ("authorisation_required", "authorisation_ref"),
        ]
        assert ledger.dispenses() == []
        [attempt] = ledger.attempts()
        assert body["attempt_id"] == attempt.id
        assert (attempt.outcome, attempt.dispense_id, attempt.medicine_id) == (
            "rejected",
            None,
            medicine.id,
        )
        assert attempt.violations == body["violations"]

    def test_the_stored_reasons_are_the_ones_the_client_was_told_down_to_their_context(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
        ledger: Ledger,
    ) -> None:
        make_dispense(
            medicine, rule, quantity=6, dispensed_at=datetime(2026, 3, 20, 10, tzinfo=UTC)
        )
        make_dispense(
            medicine, rule, quantity=20, dispensed_at=datetime(2026, 3, 21, 10, tzinfo=UTC)
        )

        response = post_dispense(client, quantity=10, dispensed_at="2026-03-10T10:00:00Z")

        body = response.json()
        assert [violation["code"] for violation in body["violations"]] == [
            "later_dispense_limit_exceeded"
        ]
        [attempt] = ledger.attempts()
        assert attempt.violations == body["violations"]
        assert attempt.violations[0]["context"]["affected"]

    def test_an_unknown_medicine_is_logged_without_a_medicine_id(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        post_dispense(client, medicine_code="NOPE")

        [attempt] = ledger.attempts()
        assert (attempt.outcome, attempt.medicine_code, attempt.medicine_id) == (
            "rejected",
            "NOPE",
            None,
        )
        assert [violation["code"] for violation in attempt.violations] == ["medicine_not_found"]
        assert ledger.dispenses() == []

    def test_the_attempt_logs_what_was_asked_for(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        post_dispense(client, quantity=11, patient_ref=" PT-9 ", authorisation_ref="AUTH-1")

        [attempt] = ledger.attempts()
        assert (
            attempt.idempotency_key,
            attempt.patient_ref,
            attempt.quantity,
            attempt.dispensed_at,
            attempt.authorisation_ref,
        ) == ("key-1", "PT-9", 11, MARCH_15, "AUTH-1")

    def test_a_rejection_uses_up_none_of_the_quota(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        refused = [
            post_dispense(client, idempotency_key=f"too-many-{number}", quantity=40)
            for number in range(3)
        ]

        accepted = [
            post_dispense(client, idempotency_key=f"fits-{number}", quantity=10)
            for number in range(3)
        ]

        assert [response.status_code for response in refused] == [422, 422, 422]
        assert [response.status_code for response in accepted] == [201, 201, 201]
        assert len(ledger.dispenses()) == 3
        assert [attempt.outcome for attempt in ledger.attempts()] == ["rejected"] * 3 + [
            "accepted"
        ] * 3


class TestEveryDecidedRequestLeavesOneRow:
    def test_one_attempt_per_idempotency_key_whatever_the_outcome(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        statuses = [
            post_dispense(client, idempotency_key="a", quantity=5).status_code,
            post_dispense(client, idempotency_key="b", quantity=50).status_code,
            post_dispense(client, idempotency_key="c", medicine_code="NOPE").status_code,
            post_dispense(client, idempotency_key="d", quantity=5).status_code,
        ]

        assert statuses == [201, 422, 422, 201]
        assert [(a.idempotency_key, a.outcome) for a in ledger.attempts()] == [
            ("a", "accepted"),
            ("b", "rejected"),
            ("c", "rejected"),
            ("d", "accepted"),
        ]
        assert len(ledger.dispenses()) == 2

    def test_a_malformed_request_is_not_a_business_attempt(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        response = post_dispense(client, quantity=0)

        assert response.status_code == 422
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])

    def test_an_attempt_row_is_never_a_dispense(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        post_dispense(client, idempotency_key="a", quantity=5)
        post_dispense(client, idempotency_key="b", quantity=50)

        accepted_ids = {a.dispense_id for a in ledger.attempts() if a.outcome == "accepted"}
        assert accepted_ids == {dispense.id for dispense in ledger.dispenses()}
