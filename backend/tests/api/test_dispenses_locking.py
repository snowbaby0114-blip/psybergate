"""The locks of the dispense transaction: the order they are taken in, and how little they hold up.

The order is what keeps two transactions from waiting for each other (SPEC 3.6): the idempotency
key, then the medicine row, then the patient and medicine. The quota is read after the last of
them, because under READ COMMITTED only a read that starts after the lock is held sees what the
previous holder committed.
"""

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app import locking, repositories
from app.config import Settings
from app.models import FormularyRule, Medicine
from tests.api.dispense_support import (
    REPLAYED,
    hold_advisory_lock,
    hold_medicine_row,
    post_dispense,
)
from tests.api.support import assert_problem

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]


@pytest.fixture
def medicine(make_medicine: MedicineFactory) -> Medicine:
    return make_medicine(code="MED-0001")


@pytest.fixture
def rule(medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
    """At most 10 per dispense and 30 per 30 days, in force since 1 January 2026."""
    return make_rule(medicine, date(2026, 1, 1))


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every lock taken and every statement run by the dispense transaction, in order."""
    recorded: list[str] = []

    def spy(module: Any, name: str, label: Callable[..., str] | None = None) -> None:
        real = getattr(module, name)

        def spied(*args: Any, **kwargs: Any) -> Any:
            recorded.append(label(*args, **kwargs) if label else name)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, spied)

    spy(repositories, "set_local_lock_timeout")
    spy(locking, "lock_idempotency_key")
    spy(repositories, "find_attempt_by_key")
    spy(
        repositories,
        "get_medicine_by_code",
        lambda session, code, lock=None: f"get_medicine_by_code(lock={lock})",
    )
    spy(locking, "lock_patient_medicine")
    spy(repositories, "list_rules")
    spy(repositories, "pair_history")
    spy(repositories, "insert_dispense")
    spy(repositories, "insert_attempt")
    spy(repositories, "get_dispense_with_medicine")
    return recorded


class TestOrder:
    def test_an_accepted_dispense_locks_then_reads_the_quota_then_records(
        self, client: TestClient, rule: FormularyRule, calls: list[str]
    ) -> None:
        assert post_dispense(client).status_code == 201

        assert calls == [
            "set_local_lock_timeout",
            "lock_idempotency_key",
            "find_attempt_by_key",
            "get_medicine_by_code(lock=share)",
            "lock_patient_medicine",
            "list_rules",
            "pair_history",
            "insert_dispense",
            "insert_attempt",
        ]

    def test_a_rejection_by_the_rules_goes_through_the_same_locks_and_logs_only_the_attempt(
        self, client: TestClient, rule: FormularyRule, calls: list[str]
    ) -> None:
        assert post_dispense(client, quantity=40).status_code == 422

        assert calls == [
            "set_local_lock_timeout",
            "lock_idempotency_key",
            "find_attempt_by_key",
            "get_medicine_by_code(lock=share)",
            "lock_patient_medicine",
            "list_rules",
            "pair_history",
            "insert_attempt",
        ]

    def test_an_unknown_medicine_has_nothing_to_lock_beyond_its_key(
        self, client: TestClient, rule: FormularyRule, calls: list[str]
    ) -> None:
        assert post_dispense(client, medicine_code="NOPE").status_code == 422

        assert calls == [
            "set_local_lock_timeout",
            "lock_idempotency_key",
            "find_attempt_by_key",
            "get_medicine_by_code(lock=share)",
            "insert_attempt",
        ]

    def test_a_replay_needs_only_its_key(
        self, client: TestClient, rule: FormularyRule, calls: list[str]
    ) -> None:
        post_dispense(client)
        calls.clear()

        assert post_dispense(client).headers[REPLAYED] == "true"

        assert calls == [
            "set_local_lock_timeout",
            "lock_idempotency_key",
            "find_attempt_by_key",
            "get_dispense_with_medicine",
        ]

    def test_the_key_mismatch_is_found_before_anything_else_is_locked(
        self, client: TestClient, rule: FormularyRule, calls: list[str]
    ) -> None:
        post_dispense(client)
        calls.clear()

        assert_problem(post_dispense(client, quantity=6), 422, "idempotency_key_reuse")

        assert calls == ["set_local_lock_timeout", "lock_idempotency_key", "find_attempt_by_key"]


class TestWhatTheLocksHoldUp:
    @pytest.fixture(autouse=True)
    def _short_lock_timeout(self, settings: Settings) -> None:
        """Anything that waits for a held lock is answered with a 503 after this long."""
        settings.lock_timeout_ms = 300

    def test_a_held_patient_lock_delays_only_that_patient_and_that_medicine(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        medicine: Medicine,
        rule: FormularyRule,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
    ) -> None:
        other = make_medicine(code="MED-0002")
        make_rule(other, date(2026, 1, 1))

        with hold_advisory_lock(session_factory, "pair", medicine.id, "PT-1"):
            same_pair = post_dispense(client, idempotency_key="same-pair")
            other_patient = post_dispense(client, idempotency_key="patient", patient_ref="PT-2")
            other_medicine = post_dispense(
                client, idempotency_key="medicine", medicine_code="MED-0002"
            )
            unknown_medicine = post_dispense(
                client, idempotency_key="unknown", medicine_code="NOPE"
            )

        assert_problem(same_pair, 503, "resource_busy")
        assert other_patient.status_code == 201
        assert other_medicine.status_code == 201
        assert unknown_medicine.status_code == 422

    def test_a_held_idempotency_key_delays_only_that_key(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        rule: FormularyRule,
    ) -> None:
        with hold_advisory_lock(session_factory, "idem", "key-1"):
            same_key = post_dispense(client, idempotency_key="key-1")
            other_key = post_dispense(client, idempotency_key="key-2")

        assert_problem(same_key, 503, "resource_busy")
        assert other_key.status_code == 201

    def test_dispenses_of_one_medicine_share_its_row(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        medicine: Medicine,
        rule: FormularyRule,
    ) -> None:
        with hold_medicine_row(session_factory, medicine.id, "SHARE"):
            response = post_dispense(client, patient_ref="PT-2")

        assert response.status_code == 201

    def test_a_change_of_rules_in_flight_holds_up_a_dispense_of_that_medicine_only(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        medicine: Medicine,
        rule: FormularyRule,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
    ) -> None:
        other = make_medicine(code="MED-0002")
        make_rule(other, date(2026, 1, 1))

        with hold_medicine_row(session_factory, medicine.id, "UPDATE"):
            held_up = post_dispense(client, idempotency_key="held-up")
            other_medicine = post_dispense(
                client, idempotency_key="other", medicine_code="MED-0002"
            )

        assert_problem(held_up, 503, "resource_busy")
        assert other_medicine.status_code == 201

    @pytest.mark.parametrize("changes", [{}, {"quantity": 40}], ids=["accepted", "rejected"])
    def test_a_replay_does_not_wait_for_the_patient_or_for_a_change_of_rules(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        medicine: Medicine,
        rule: FormularyRule,
        changes: dict[str, Any],
    ) -> None:
        first = post_dispense(client, **changes)

        with (
            hold_advisory_lock(session_factory, "pair", medicine.id, "PT-1"),
            hold_medicine_row(session_factory, medicine.id, "UPDATE"),
        ):
            replay = post_dispense(client, **changes)

        assert replay.status_code == first.status_code
        assert replay.headers[REPLAYED] == "true"
