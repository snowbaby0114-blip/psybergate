"""What run_seed stores, read back from PostgreSQL and held to the documented guarantees."""

import re
import uuid
from collections import defaultdict
from collections.abc import Iterator
from datetime import timedelta
from itertools import pairwise
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

from app.domain.dispense import (
    AUTHORISATION_REQUIRED,
    MAX_QUANTITY_PER_30_DAYS_EXCEEDED,
    MAX_QUANTITY_PER_DISPENSE_EXCEEDED,
    MEDICINE_INACTIVE,
)
from app.domain.idempotency import request_fingerprint
from app.domain.rules import rule_status
from app.domain.timeutil import local_day_start_utc, to_local_date
from app.seed import (
    DEMO_AUTH,
    DEMO_HISTORY,
    DEMO_INACTIVE,
    DEMO_PATIENT,
    DEMO_TIGHT,
    SEED_NAMESPACE,
    SeedSummary,
)
from tests.seed.support import (
    DISPENSES,
    MEDICINES,
    PATIENTS,
    RANDOM_SEED,
    TODAY,
    YEARS,
    engine_disagreements,
    evaluate_stored,
    rows,
    stored_rules,
)

DEMO_MEDICINES = 4
DEMO_DISPENSES = 10
NOON_TODAY = local_day_start_utc(TODAY) + timedelta(hours=12)


@pytest.fixture
def connection(engine: Engine, seeded: SeedSummary) -> Iterator[Connection]:
    with engine.connect() as connection:
        yield connection


def count(connection: Connection, sql: str) -> int:
    return connection.execute(text(sql)).scalar_one()


class TestCatalogue:
    def test_the_requested_counts_are_stored_on_top_of_the_demo_fixtures(
        self, connection: Connection, seeded: SeedSummary
    ) -> None:
        assert count(connection, "SELECT count(*) FROM medicines") == MEDICINES + DEMO_MEDICINES
        assert count(connection, "SELECT count(*) FROM dispenses") == DISPENSES + DEMO_DISPENSES
        assert count(connection, "SELECT count(DISTINCT patient_ref) FROM dispenses") == (
            PATIENTS + 1
        )
        assert seeded.expected.medicines == MEDICINES + DEMO_MEDICINES
        assert seeded.expected.dispenses == DISPENSES + DEMO_DISPENSES
        assert seeded.expected.patients == PATIENTS + 1

    def test_codes_are_numbered_and_the_demo_codes_are_present(
        self, connection: Connection
    ) -> None:
        codes = [row["code"] for row in rows(connection, "SELECT code FROM medicines ORDER BY id")]

        assert codes[:MEDICINES] == [f"MED-{number:05d}" for number in range(1, MEDICINES + 1)]
        assert set(codes[MEDICINES:]) == {DEMO_AUTH, DEMO_TIGHT, DEMO_INACTIVE, DEMO_HISTORY}

    def test_every_medicine_has_three_to_five_rules_tiling_without_gaps_or_overlaps(
        self, connection: Connection
    ) -> None:
        per_medicine = defaultdict(list)
        for row in rows(connection, "SELECT * FROM formulary_rules ORDER BY effective_from"):
            per_medicine[row["medicine_id"]].append(row)

        assert len(per_medicine) == MEDICINES + DEMO_MEDICINES
        for periods in per_medicine.values():
            assert 3 <= len(periods) <= 5
            for earlier, later in pairwise(periods):
                assert earlier["effective_to"] == later["effective_from"]
            assert periods[-1]["effective_to"] is None
            first_start = periods[0]["effective_from"]
            assert first_start <= TODAY - timedelta(days=YEARS * 365 + 183)

    def test_a_tenth_of_the_medicines_have_a_rule_scheduled_to_start_ten_to_ninety_days_ahead(
        self, connection: Connection
    ) -> None:
        scheduled = rows(
            connection,
            "SELECT m.code, r.effective_from FROM formulary_rules r"
            " JOIN medicines m ON m.id = r.medicine_id"
            " WHERE r.effective_from > :today",
            today=TODAY,
        )

        assert len(scheduled) == round(MEDICINES * 0.10) + 1  # the demo history medicine too
        for row in scheduled:
            assert 10 <= (row["effective_from"] - TODAY).days <= 90

    def test_the_timelines_show_rules_that_are_expired_in_force_and_scheduled(
        self, connection: Connection
    ) -> None:
        states = {
            rule_status(rule, TODAY)
            for rules in stored_rules(connection).values()
            for rule in rules
        }

        assert states == {"expired", "in_force", "scheduled"}

    def test_five_percent_of_the_generated_medicines_are_inactive_and_never_dispensed(
        self, connection: Connection
    ) -> None:
        inactive = rows(connection, "SELECT code FROM medicines WHERE NOT is_active")

        assert len(inactive) == round(MEDICINES * 0.05) + 1  # the demo inactive medicine too
        assert (
            count(
                connection,
                "SELECT count(*) FROM dispenses d JOIN medicines m ON m.id = d.medicine_id"
                " WHERE NOT m.is_active",
            )
            == 0
        )

    def test_roughly_fifteen_percent_of_the_rules_require_authorisation(
        self, connection: Connection
    ) -> None:
        required = count(
            connection, "SELECT count(*) FROM formulary_rules WHERE requires_authorisation"
        )
        total = count(connection, "SELECT count(*) FROM formulary_rules")

        assert 0.05 < required / total < 0.30


class TestDispenses:
    def test_the_engine_accepts_every_stored_dispense_under_its_stored_rule(
        self, connection: Connection
    ) -> None:
        assert engine_disagreements(connection) == []

    def test_patient_refs_are_opaque_and_ids_grow_with_time(self, connection: Connection) -> None:
        stored = rows(connection, "SELECT id, patient_ref, dispensed_at FROM dispenses ORDER BY id")

        refs = {row["patient_ref"] for row in stored}
        assert DEMO_PATIENT in refs
        assert all(re.fullmatch(r"PT-[A-Z2-7]{8}", ref) for ref in refs - {DEMO_PATIENT})
        instants = [row["dispensed_at"] for row in stored]
        assert instants == sorted(instants)

    def test_the_history_spans_the_two_year_window_and_ends_before_today_begins(
        self, connection: Connection
    ) -> None:
        stored = rows(connection, "SELECT dispensed_at FROM dispenses")
        days = [to_local_date(row["dispensed_at"]) for row in stored]

        assert min(days) <= TODAY - timedelta(days=YEARS * 365 - 7)
        assert max(days) >= TODAY - timedelta(days=7)
        assert max(row["dispensed_at"] for row in stored) < local_day_start_utc(TODAY)

    def test_some_dispenses_fall_on_a_business_day_after_their_utc_date(
        self, connection: Connection
    ) -> None:
        stored = rows(connection, "SELECT dispensed_at FROM dispenses")

        different = [
            row
            for row in stored
            if row["dispensed_at"].date() != to_local_date(row["dispensed_at"])
        ]
        assert different

    def test_repeat_dispenses_of_one_medicine_fall_within_a_single_30_day_window(
        self, connection: Connection
    ) -> None:
        dates: dict[tuple[str, int], list[Any]] = defaultdict(list)
        for row in rows(connection, "SELECT patient_ref, medicine_id, dispensed_at FROM dispenses"):
            dates[(row["patient_ref"], row["medicine_id"])].append(
                to_local_date(row["dispensed_at"])
            )

        close = sum(
            any(later - earlier < timedelta(days=30) for earlier, later in pairwise(sorted(days)))
            for days in dates.values()
        )
        assert close >= 5


class TestAttemptLog:
    def test_every_dispense_has_exactly_one_accepted_attempt_that_mirrors_it(
        self, connection: Connection
    ) -> None:
        mirrored = count(
            connection,
            "SELECT count(*) FROM dispenses d JOIN dispense_attempts a ON a.dispense_id = d.id"
            " WHERE a.outcome = 'accepted' AND a.violations = '[]'::jsonb"
            " AND a.idempotency_key = d.idempotency_key AND a.patient_ref = d.patient_ref"
            " AND a.medicine_id = d.medicine_id AND a.quantity = d.quantity"
            " AND a.dispensed_at = d.dispensed_at"
            " AND a.authorisation_ref IS NOT DISTINCT FROM d.authorisation_ref"
            " AND a.created_at = d.created_at",
        )

        assert mirrored == DISPENSES + DEMO_DISPENSES
        assert count(connection, "SELECT count(*) FROM dispense_attempts") == mirrored

    def test_the_request_hash_is_the_fingerprint_the_api_computes_for_the_same_request(
        self, connection: Connection
    ) -> None:
        stored = rows(
            connection,
            "SELECT m.code, d.patient_ref, d.quantity, d.dispensed_at, d.authorisation_ref,"
            " a.request_hash FROM dispenses d"
            " JOIN medicines m ON m.id = d.medicine_id"
            " JOIN dispense_attempts a ON a.dispense_id = d.id",
        )

        for row in stored:
            assert row["request_hash"] == request_fingerprint(
                medicine_code=row["code"],
                patient_ref=row["patient_ref"],
                quantity=row["quantity"],
                dispensed_at=row["dispensed_at"],
                authorisation_ref=row["authorisation_ref"],
            )

    def test_idempotency_keys_are_seed_prefixed_uuid5_values_of_the_seed_and_an_index(
        self, connection: Connection
    ) -> None:
        keys = {
            row["idempotency_key"]
            for row in rows(connection, "SELECT idempotency_key FROM dispenses")
        }

        assert keys == {
            f"seed-{uuid.uuid5(SEED_NAMESPACE, f'{RANDOM_SEED}:{index}')}"
            for index in range(DISPENSES + DEMO_DISPENSES)
        }

    def test_a_dispense_is_logged_seconds_to_minutes_after_it_happened(
        self, connection: Connection
    ) -> None:
        stored = rows(connection, "SELECT dispensed_at, created_at FROM dispenses")

        delays = [row["created_at"] - row["dispensed_at"] for row in stored]
        assert min(delays) >= timedelta(seconds=3)
        assert max(delays) <= timedelta(minutes=20)
        assert max(row["created_at"] for row in stored) < local_day_start_utc(TODAY)


class TestDemoFixtures:
    def test_demo_auth_requires_authorisation_in_every_period_and_refuses_a_dispense_without(
        self, connection: Connection
    ) -> None:
        rules = rows(
            connection,
            "SELECT r.requires_authorisation FROM formulary_rules r"
            " JOIN medicines m ON m.id = r.medicine_id WHERE m.code = :code",
            code=DEMO_AUTH,
        )
        without = evaluate_stored(
            connection, patient_ref="PT-NEW", code=DEMO_AUTH, quantity=5, at=NOON_TODAY
        )
        with_reference = evaluate_stored(
            connection,
            patient_ref="PT-NEW",
            code=DEMO_AUTH,
            quantity=5,
            at=NOON_TODAY,
            authorisation_ref="AUTH-1",
        )

        assert all(rule["requires_authorisation"] for rule in rules)
        assert [v.code for v in without.violations] == [AUTHORISATION_REQUIRED]
        assert with_reference.accepted

    def test_demo_tight_has_room_for_two_more_units_today_and_no_more(
        self, connection: Connection
    ) -> None:
        def evaluate(quantity: int):
            return evaluate_stored(
                connection,
                patient_ref=DEMO_PATIENT,
                code=DEMO_TIGHT,
                quantity=quantity,
                at=NOON_TODAY,
            )

        assert evaluate(2).accepted
        assert [v.code for v in evaluate(3).violations] == [MAX_QUANTITY_PER_30_DAYS_EXCEEDED]
        assert [v.code for v in evaluate(11).violations] == [
            MAX_QUANTITY_PER_DISPENSE_EXCEEDED,
            MAX_QUANTITY_PER_30_DAYS_EXCEEDED,
        ]

    def test_demo_inactive_refuses_every_dispense(self, connection: Connection) -> None:
        evaluation = evaluate_stored(
            connection, patient_ref=DEMO_PATIENT, code=DEMO_INACTIVE, quantity=1, at=NOON_TODAY
        )

        assert [v.code for v in evaluation.violations] == [MEDICINE_INACTIVE]

    def test_demo_history_has_five_periods_and_two_dispenses_either_side_of_a_midnight(
        self, connection: Connection
    ) -> None:
        medicine = rows(connection, "SELECT id FROM medicines WHERE code = :c", c=DEMO_HISTORY)[0]
        rules = stored_rules(connection)[medicine["id"]]
        around_midnight = rows(
            connection,
            "SELECT rule_id, dispensed_at FROM dispenses WHERE medicine_id = :id"
            " AND patient_ref = :patient ORDER BY dispensed_at DESC LIMIT 2",
            id=medicine["id"],
            patient=DEMO_PATIENT,
        )
        after, before = around_midnight

        assert [rule_status(rule, TODAY) for rule in rules] == [
            "expired",
            "expired",
            "expired",
            "in_force",
            "scheduled",
        ]
        assert (before["rule_id"], after["rule_id"]) == (rules[2].id, rules[3].id)
        assert before["dispensed_at"].date() == after["dispensed_at"].date()
        assert to_local_date(after["dispensed_at"]) == rules[3].effective_from

    def test_pt_demo_has_dispenses_of_the_three_active_demo_medicines(
        self, connection: Connection
    ) -> None:
        codes = {
            row["code"]
            for row in rows(
                connection,
                "SELECT DISTINCT m.code FROM dispenses d JOIN medicines m ON m.id = d.medicine_id"
                " WHERE d.patient_ref = :patient",
                patient=DEMO_PATIENT,
            )
        }

        assert codes == {DEMO_AUTH, DEMO_TIGHT, DEMO_HISTORY}
