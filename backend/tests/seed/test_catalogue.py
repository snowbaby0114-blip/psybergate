"""The generated catalogue: medicines and their rule timelines. Pure, so no database is needed."""

from datetime import date, timedelta
from itertools import pairwise
from random import Random

import pytest

from app.domain.rules import RuleVersion, rule_status
from app.domain.timeutil import local_day_start_utc
from app.seed import (
    DEMO_AUTH,
    DEMO_HISTORY,
    DEMO_INACTIVE,
    DEMO_TIGHT,
    MedicineSpec,
    generate_catalogue,
)

TODAY = date(2026, 6, 15)
YEARS = 2
DEMO_CODES = (DEMO_AUTH, DEMO_TIGHT, DEMO_INACTIVE, DEMO_HISTORY)
UNIT_OF_FORM = {
    "tablet": "mg",
    "capsule": "mg",
    "suspension": "mg/5 mL",
    "injection": "mg/mL",
    "cream": "%",
    "inhaler": "mcg/dose",
}


def catalogue(
    count: int = 200, *, seed: str = "catalogue", years: int = YEARS
) -> list[MedicineSpec]:
    return generate_catalogue(Random(seed), count, TODAY, years)


def generated(medicines: list[MedicineSpec]) -> list[MedicineSpec]:
    return [medicine for medicine in medicines if medicine.code not in DEMO_CODES]


def by_code(medicines: list[MedicineSpec], code: str) -> MedicineSpec:
    return next(medicine for medicine in medicines if medicine.code == code)


def statuses(medicine: MedicineSpec) -> list[str]:
    return [
        rule_status(
            RuleVersion(
                id=number,
                effective_from=period.effective_from,
                effective_to=period.effective_to,
                max_quantity_per_dispense=period.limits.max_quantity_per_dispense,
                max_quantity_per_30_days=period.limits.max_quantity_per_30_days,
                requires_authorisation=period.limits.requires_authorisation,
            ),
            TODAY,
        )
        for number, period in enumerate(medicine.periods)
    ]


class TestMedicines:
    def test_there_are_the_requested_number_of_generated_medicines_plus_the_demo_fixtures(
        self,
    ) -> None:
        medicines = catalogue(200)

        assert len(medicines) == 204
        assert [medicine.code for medicine in generated(medicines)] == [
            f"MED-{number:05d}" for number in range(1, 201)
        ]
        assert {medicine.code for medicine in medicines} >= set(DEMO_CODES)

    def test_codes_are_unique_and_names_are_generated_words_with_some_variety(self) -> None:
        medicines = catalogue(500)

        assert len({medicine.code for medicine in medicines}) == len(medicines)
        names = [medicine.name for medicine in generated(medicines)]
        assert all(name.isalpha() and name.istitle() for name in names)
        assert len(set(names)) > 300

    def test_every_form_comes_with_a_plausible_unit_and_a_positive_strength(self) -> None:
        medicines = catalogue(500)

        assert {medicine.form for medicine in medicines} == set(UNIT_OF_FORM)
        for medicine in medicines:
            assert medicine.strength_unit == UNIT_OF_FORM[medicine.form]
            assert medicine.strength_value > 0

    def test_exactly_the_requested_share_of_generated_medicines_is_inactive(self) -> None:
        medicines = catalogue(200)

        assert sum(not medicine.is_active for medicine in generated(medicines)) == 10

    def test_a_catalogue_of_one_is_still_valid_and_has_an_active_generated_medicine(self) -> None:
        medicines = catalogue(1)

        assert [medicine.code for medicine in generated(medicines)] == ["MED-00001"]
        assert generated(medicines)[0].is_active


class TestRuleTimelines:
    @pytest.mark.parametrize("years", [1, 2, 5])
    def test_every_medicine_has_three_to_five_contiguous_periods_ending_open(
        self, years: int
    ) -> None:
        for medicine in catalogue(150, years=years):
            periods = medicine.periods
            assert 3 <= len(periods) <= 5, medicine.code
            for earlier, later in pairwise(periods):
                assert earlier.effective_to == later.effective_from, medicine.code
            assert periods[-1].effective_to is None, medicine.code

    @pytest.mark.parametrize("years", [1, 2, 5])
    def test_the_first_period_starts_at_least_two_and_a_half_years_before_the_window_it_serves(
        self, years: int
    ) -> None:
        window_start = TODAY - timedelta(days=years * 365)

        for medicine in catalogue(150, years=years):
            # The 30-day window of the oldest dispense reaches 29 days further back.
            assert medicine.periods[0].effective_from <= window_start - timedelta(days=183)

    def test_periods_are_at_least_thirty_days_long(self) -> None:
        for medicine in catalogue(300):
            for period in medicine.periods[:-1]:
                assert period.effective_to is not None
                assert (period.effective_to - period.effective_from).days >= 30, medicine.code

    def test_the_scheduled_share_has_its_last_period_starting_ten_to_ninety_days_ahead(
        self,
    ) -> None:
        medicines = catalogue(200)

        scheduled = [m for m in generated(medicines) if m.periods[-1].effective_from > TODAY]
        assert len(scheduled) == 20
        for medicine in scheduled:
            ahead = (medicine.periods[-1].effective_from - TODAY).days
            assert 10 <= ahead <= 90, medicine.code
        for medicine in set(generated(medicines)) - set(scheduled):
            assert medicine.periods[-1].effective_from <= TODAY - timedelta(days=20)

    def test_a_scheduled_medicine_shows_expired_in_force_and_scheduled_rules(self) -> None:
        medicines = catalogue(200)

        scheduled = [m for m in generated(medicines) if m.periods[-1].effective_from > TODAY]
        for medicine in scheduled:
            found = statuses(medicine)
            assert found[-1] == "scheduled"
            assert found[-2] == "in_force"
            assert set(found[:-2]) == {"expired"}

    def test_a_medicine_without_a_scheduled_period_is_governed_by_its_last_open_one(self) -> None:
        medicines = catalogue(200)

        current = [m for m in generated(medicines) if m.periods[-1].effective_from <= TODAY]
        assert current
        for medicine in current:
            assert statuses(medicine)[-1] == "in_force"

    def test_limits_vary_and_the_monthly_cap_is_usually_but_not_always_the_larger(self) -> None:
        limits = [p.limits for m in catalogue(500) for p in m.periods]

        assert len({limit.max_quantity_per_dispense for limit in limits}) >= 8
        smaller_monthly_cap = [
            limit
            for limit in limits
            if limit.max_quantity_per_30_days < limit.max_quantity_per_dispense
        ]
        assert 0 < len(smaller_monthly_cap) < len(limits) * 0.2
        assert all(limit.max_quantity_per_30_days >= 1 for limit in limits)

    def test_about_fifteen_percent_of_periods_require_authorisation(self) -> None:
        limits = [p.limits for m in generated(catalogue(500)) for p in m.periods]

        share = sum(limit.requires_authorisation for limit in limits) / len(limits)
        assert 0.11 < share < 0.19

    def test_rules_are_entered_before_they_take_effect_and_never_after_today_began(self) -> None:
        today_begins = local_day_start_utc(TODAY)

        for medicine in catalogue(200):
            assert medicine.created_at < medicine.periods[0].created_at
            for period in medicine.periods:
                assert period.created_at < local_day_start_utc(period.effective_from) or (
                    period.effective_from > TODAY
                )
                assert period.created_at < today_begins


class TestDemoFixtures:
    def test_demo_auth_requires_authorisation_in_every_period(self) -> None:
        demo = by_code(catalogue(), DEMO_AUTH)

        assert demo.is_active
        assert all(period.limits.requires_authorisation for period in demo.periods)

    def test_demo_tight_has_small_limits_in_force(self) -> None:
        demo = by_code(catalogue(), DEMO_TIGHT)

        current = demo.periods[-1].limits
        assert (current.max_quantity_per_dispense, current.max_quantity_per_30_days) == (10, 20)
        assert demo.periods[-1].effective_from < TODAY - timedelta(days=60)

    def test_demo_inactive_is_the_only_inactive_demo_medicine(self) -> None:
        medicines = catalogue()

        assert not by_code(medicines, DEMO_INACTIVE).is_active
        assert all(
            by_code(medicines, code).is_active for code in (DEMO_AUTH, DEMO_TIGHT, DEMO_HISTORY)
        )

    def test_demo_history_has_five_periods_in_every_state(self) -> None:
        demo = by_code(catalogue(), DEMO_HISTORY)

        assert len(demo.periods) == 5
        assert statuses(demo) == ["expired", "expired", "expired", "in_force", "scheduled"]
        assert demo.periods[0].effective_from <= TODAY - timedelta(days=YEARS * 365 + 183)


class TestDeterminism:
    def test_the_same_random_stream_gives_the_same_catalogue(self) -> None:
        assert catalogue(100, seed="same") == catalogue(100, seed="same")

    def test_another_random_stream_gives_another_catalogue(self) -> None:
        assert catalogue(100, seed="one") != catalogue(100, seed="two")
