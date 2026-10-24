"""Planning and admitting dispenses with the real engine. Pure: ids are made up, no database."""

import re
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from random import Random

import pytest

from app import seed
from app.domain.timeutil import TZ, local_day_start_utc, to_local_date
from app.seed import (
    DEMO_AUTH,
    DEMO_HISTORY,
    DEMO_PATIENT,
    DEMO_TIGHT,
    SeedError,
    generate_dispenses,
    generate_patient_refs,
)
from tests.seed.support import (
    TODAY,
    PureDataset,
    broken_rules,
    build_pure_dataset,
    pairs_with_dispenses_inside_one_window,
    rule_of,
)

DEMO_DISPENSES = 10
REQUESTED = 6000


@pytest.fixture(scope="module")
def dataset() -> PureDataset:
    return build_pure_dataset(medicines=500, dispenses=REQUESTED, patients=2500)


class TestPatientRefs:
    def test_refs_are_distinct_opaque_and_prefixed(self) -> None:
        refs = generate_patient_refs(Random(1), 5000)

        assert len(set(refs)) == 5000
        assert all(re.fullmatch(r"PT-[A-Z2-7]{8}", ref) for ref in refs)
        assert DEMO_PATIENT not in refs

    def test_refs_are_reproducible(self) -> None:
        first = generate_patient_refs(Random(7), 50)
        again = generate_patient_refs(Random(7), 50)
        other = generate_patient_refs(Random(8), 50)

        assert first == again
        assert first != other


class TestAdmission:
    def test_the_requested_number_is_admitted_on_top_of_the_demo_dispenses(
        self, dataset: PureDataset
    ) -> None:
        assert len(dataset.ledger.admitted) == REQUESTED + DEMO_DISPENSES

    def test_every_patient_has_at_least_one_dispense(self, dataset: PureDataset) -> None:
        with_dispenses = {dispense.patient_ref for dispense in dataset.ledger.admitted}

        assert with_dispenses == {*dataset.patients, DEMO_PATIENT}

    def test_no_admitted_dispense_breaks_a_rule(self, dataset: PureDataset) -> None:
        # Worked out from the rules and business dates alone, not by asking the engine again.
        assert broken_rules(dataset.entries, dataset.ledger.admitted) == []

    def test_the_engine_turns_candidates_away_and_says_why(self, dataset: PureDataset) -> None:
        ledger = dataset.ledger

        assert ledger.skipped > 0
        assert set(ledger.skipped_reasons) <= {
            "max_quantity_per_30_days_exceeded",
            "later_dispense_limit_exceeded",
        }

    def test_dispenses_are_dated_inside_the_window_and_end_before_today_begins(
        self, dataset: PureDataset
    ) -> None:
        days = [to_local_date(d.dispensed_at) for d in dataset.ledger.admitted]

        assert min(days) <= dataset.window.start + timedelta(days=7)
        assert max(days) >= TODAY - timedelta(days=7)
        assert dataset.window.start <= min(days)
        assert max(d.dispensed_at for d in dataset.ledger.admitted) < local_day_start_utc(TODAY)

    def test_generated_instants_are_whole_seconds_in_opening_hours_or_just_after_midnight(
        self, dataset: PureDataset
    ) -> None:
        generated = [d for d in dataset.ledger.admitted if d.patient_ref != DEMO_PATIENT]

        for dispense in generated:
            local = dispense.dispensed_at.astimezone(TZ)
            assert dispense.dispensed_at.microsecond == 0
            assert local.hour < 2 or 8 <= local.hour < 18

    def test_some_dispenses_belong_to_a_business_day_other_than_their_utc_date(
        self, dataset: PureDataset
    ) -> None:
        night = [
            d
            for d in dataset.ledger.admitted
            if d.dispensed_at.date() != to_local_date(d.dispensed_at)
        ]

        assert len(night) > 100

    def test_some_dispenses_are_governed_by_a_rule_that_began_at_the_midnight_before_them(
        self, dataset: PureDataset
    ) -> None:
        after_midnight = [
            d
            for d in dataset.ledger.admitted
            if rule_of(dataset.entries, d).effective_from == to_local_date(d.dispensed_at)
            and d.dispensed_at.date() != to_local_date(d.dispensed_at)
        ]

        assert len(after_midnight) > 20

    def test_repeat_dispenses_of_one_medicine_fall_inside_a_single_30_day_window(
        self, dataset: PureDataset
    ) -> None:
        assert pairs_with_dispenses_inside_one_window(dataset.ledger.admitted) > 500

    def test_most_patients_have_one_dispense_and_a_few_have_many(
        self, dataset: PureDataset
    ) -> None:
        per_patient = Counter(d.patient_ref for d in dataset.ledger.admitted)

        assert sum(1 for count in per_patient.values() if count == 1) > len(per_patient) / 3
        assert max(per_patient.values()) >= 10

    def test_popular_medicines_are_dispensed_far_more_often_than_unpopular_ones(
        self, dataset: PureDataset
    ) -> None:
        per_medicine = Counter(d.medicine.id for d in dataset.ledger.admitted)

        assert max(per_medicine.values()) > 10 * (REQUESTED / 500)
        assert len(per_medicine) > 400

    def test_authorisation_is_always_given_where_required_and_sometimes_where_not(
        self, dataset: PureDataset
    ) -> None:
        required = [
            d for d in dataset.ledger.admitted if rule_of(dataset.entries, d).requires_authorisation
        ]
        optional = [
            d
            for d in dataset.ledger.admitted
            if not rule_of(dataset.entries, d).requires_authorisation
        ]

        assert required
        assert all(d.authorisation_ref for d in required)
        assert 0 < sum(d.authorisation_ref is not None for d in optional) < len(optional) / 4
        references = [d.authorisation_ref for d in dataset.ledger.admitted if d.authorisation_ref]
        assert all(re.fullmatch(r"AUTH-([A-Z2-7]{6}|D\d)", ref) for ref in references)


class TestDemoFixtures:
    def test_pt_demo_has_dispenses_from_the_window_opening_to_two_days_ago(
        self, dataset: PureDataset
    ) -> None:
        demo = [d for d in dataset.ledger.admitted if d.patient_ref == DEMO_PATIENT]
        days = sorted(to_local_date(d.dispensed_at) for d in demo)

        assert len(demo) == DEMO_DISPENSES
        assert days[0] == dataset.window.start + timedelta(days=1)
        assert days[-1] == TODAY - timedelta(days=2)

    def test_pt_demo_has_used_18_of_the_20_units_of_demo_tight_in_the_last_30_days(
        self, dataset: PureDataset
    ) -> None:
        tight = [
            d
            for d in dataset.ledger.admitted
            if d.patient_ref == DEMO_PATIENT and d.medicine.code == DEMO_TIGHT
        ]
        in_last_30_days = [
            d for d in tight if to_local_date(d.dispensed_at) >= TODAY - timedelta(days=29)
        ]

        assert len(in_last_30_days) == 3
        assert sum(d.quantity for d in in_last_30_days) == 18
        assert rule_of(dataset.entries, tight[-1]).max_quantity_per_30_days == 20

    def test_pt_demo_has_two_dispenses_either_side_of_the_midnight_a_rule_begins(
        self, dataset: PureDataset
    ) -> None:
        history = sorted(
            (
                d
                for d in dataset.ledger.admitted
                if d.patient_ref == DEMO_PATIENT and d.medicine.code == DEMO_HISTORY
            ),
            key=lambda d: d.dispensed_at,
        )
        before, after = history[-2], history[-1]
        new_rule = rule_of(dataset.entries, after)

        assert after.dispensed_at - before.dispensed_at == timedelta(hours=1)
        assert before.dispensed_at.date() == after.dispensed_at.date()
        assert to_local_date(after.dispensed_at) - to_local_date(before.dispensed_at) == timedelta(
            days=1
        )
        assert new_rule.effective_from == to_local_date(after.dispensed_at)
        assert rule_of(dataset.entries, before).effective_to == new_rule.effective_from
        assert rule_of(dataset.entries, before).requires_authorisation
        assert before.authorisation_ref is not None
        assert not new_rule.requires_authorisation

    def test_demo_auth_dispenses_carry_their_authorisation_reference(
        self, dataset: PureDataset
    ) -> None:
        demo_auth = [d for d in dataset.ledger.admitted if d.medicine.code == DEMO_AUTH]

        assert len(demo_auth) == 2
        assert all(d.authorisation_ref for d in demo_auth)


class TestDeterminismAndFailure:
    def test_the_same_seed_gives_the_same_ledger_and_another_seed_another(self) -> None:
        first = build_pure_dataset(medicines=60, dispenses=300, patients=150, seed="a")
        again = build_pure_dataset(medicines=60, dispenses=300, patients=150, seed="a")
        other = build_pure_dataset(medicines=60, dispenses=300, patients=150, seed="b")

        assert first.ledger.admitted == again.ledger.admitted
        assert first.ledger.admitted != other.ledger.admitted

    def test_rejected_visits_are_replaced_until_the_requested_number_is_reached(self) -> None:
        # Two patients with 40 dispenses each saturate their 30-day windows, so many are refused.
        crowded = build_pure_dataset(medicines=30, dispenses=80, patients=2)

        assert crowded.ledger.skipped > 0
        assert len(crowded.ledger.admitted) == 80 + DEMO_DISPENSES
        assert broken_rules(crowded.entries, crowded.ledger.admitted) == []

    def test_it_gives_up_with_a_clear_error_when_the_rounds_run_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(seed, "MAX_ROUNDS", 1)

        with pytest.raises(SeedError, match=r"\d+ of 80 dispenses could not be placed"):
            build_pure_dataset(medicines=30, dispenses=80, patients=2)

    def test_a_demo_dispense_the_engine_rejects_is_an_error_not_a_silent_gap(self) -> None:
        good = build_pure_dataset(medicines=20, dispenses=40, patients=20)
        entries = [
            replace(entry, is_active=False) if entry.code == DEMO_TIGHT else entry
            for entry in good.entries
        ]

        with pytest.raises(SeedError, match="medicine_inactive"):
            generate_dispenses(Random(1), entries, good.patients, 40, good.window)
