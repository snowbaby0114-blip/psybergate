import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.domain.dispense import (
    AUTHORISATION_REQUIRED,
    LATER_DISPENSE_LIMIT_EXCEEDED,
    MAX_QUANTITY_PER_30_DAYS_EXCEEDED,
    MAX_QUANTITY_PER_DISPENSE_EXCEEDED,
    MEDICINE_INACTIVE,
    MEDICINE_NOT_FOUND,
    NO_RULE_IN_FORCE,
    Candidate,
    Evaluation,
    PriorDispense,
    evaluate_dispense,
    medicine_not_found,
)
from app.domain.rules import RuleVersion
from app.domain.timeutil import history_bounds_utc


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def make_rule(
    rule_id: int = 1,
    start: date = date(2026, 1, 1),
    end: date | None = None,
    *,
    per_dispense: int = 100,
    per_30_days: int = 100,
    requires_authorisation: bool = False,
) -> RuleVersion:
    return RuleVersion(
        id=rule_id,
        effective_from=start,
        effective_to=end,
        max_quantity_per_dispense=per_dispense,
        max_quantity_per_30_days=per_30_days,
        requires_authorisation=requires_authorisation,
    )


def prior(dispense_id: int, instant: datetime, quantity: int) -> PriorDispense:
    return PriorDispense(id=dispense_id, dispensed_at=instant, quantity=quantity)


def evaluate(
    candidate: Candidate,
    *,
    rules: Sequence[RuleVersion] | None = None,
    history: Sequence[PriorDispense] = (),
    medicine_active: bool = True,
) -> Evaluation:
    return evaluate_dispense(
        medicine_active=medicine_active,
        rules=[make_rule()] if rules is None else rules,
        history=history,
        candidate=candidate,
    )


def codes(evaluation: Evaluation) -> list[str]:
    return [violation.code for violation in evaluation.violations]


NOON = utc(2026, 3, 15, 10)


class TestAcceptance:
    def test_accepts_a_dispense_that_breaks_no_rule_and_reports_the_governing_rule(self) -> None:
        rule = make_rule(7, per_dispense=50, per_30_days=120)

        evaluation = evaluate(Candidate(NOON, 20), rules=[rule])

        assert evaluation.accepted
        assert evaluation.violations == ()
        assert evaluation.rule == rule

    def test_a_quantity_equal_to_the_per_dispense_limit_is_allowed_but_one_more_is_not(
        self,
    ) -> None:
        rules = [make_rule(per_dispense=50, per_30_days=500)]

        assert evaluate(Candidate(NOON, 50), rules=rules).accepted
        assert codes(evaluate(Candidate(NOON, 51), rules=rules)) == [
            MAX_QUANTITY_PER_DISPENSE_EXCEEDED
        ]

    def test_reaching_the_30_day_limit_exactly_is_allowed_but_one_more_is_not(self) -> None:
        history = [prior(1, utc(2026, 3, 1, 10), 60)]

        assert evaluate(Candidate(NOON, 40), history=history).accepted
        assert codes(evaluate(Candidate(NOON, 41), history=history)) == [
            MAX_QUANTITY_PER_30_DAYS_EXCEEDED
        ]

    def test_a_rule_effective_on_a_sast_day_governs_dispenses_from_22_00_utc_the_day_before(
        self,
    ) -> None:
        rules = [make_rule(5, date(2026, 3, 1))]

        accepted = evaluate(Candidate(utc(2026, 2, 28, 22, 30), 10), rules=rules)
        rejected = evaluate(Candidate(utc(2026, 2, 28, 21, 59, 59), 10), rules=rules)

        assert accepted.accepted
        assert accepted.rule == rules[0]
        assert codes(rejected) == [NO_RULE_IN_FORCE]
        assert rejected.violations[0].context == {"business_date": "2026-02-28"}

    def test_the_candidate_may_be_expressed_in_any_utc_offset(self) -> None:
        rules = [make_rule(5, date(2026, 3, 1))]
        local = datetime(2026, 3, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))

        assert evaluate(Candidate(local, 10), rules=rules).accepted

    def test_a_dispense_backdated_into_an_earlier_rule_period_uses_that_periods_limits(
        self,
    ) -> None:
        old = make_rule(1, date(2026, 1, 1), date(2026, 3, 1), per_dispense=100)
        new = make_rule(2, date(2026, 3, 1), per_dispense=20)

        backdated = evaluate(Candidate(utc(2026, 2, 10, 10), 80), rules=[new, old])
        current = evaluate(Candidate(utc(2026, 3, 10, 10), 80), rules=[new, old])

        assert backdated.accepted
        assert backdated.rule == old
        assert codes(current) == [MAX_QUANTITY_PER_DISPENSE_EXCEEDED]
        assert current.rule == new


class TestEachRuleAlone:
    def test_inactive_medicine(self) -> None:
        evaluation = evaluate(Candidate(NOON, 10), medicine_active=False)

        [violation] = evaluation.violations
        assert violation.code == MEDICINE_INACTIVE == "medicine_inactive"
        assert violation.field == "medicine_code"
        assert violation.context == {}
        assert "inactive" in violation.message
        assert evaluation.rule is not None

    def test_no_rule_in_force(self) -> None:
        rules = [make_rule(1, date(2026, 3, 16))]

        evaluation = evaluate(Candidate(NOON, 10), rules=rules)

        [violation] = evaluation.violations
        assert violation.code == NO_RULE_IN_FORCE == "no_rule_in_force"
        assert violation.field == "dispensed_at"
        assert violation.context == {"business_date": "2026-03-15"}
        assert "2026-03-15" in violation.message
        assert evaluation.rule is None
        assert not evaluation.accepted

    def test_no_rules_at_all(self) -> None:
        assert codes(evaluate(Candidate(NOON, 10), rules=[])) == [NO_RULE_IN_FORCE]

    def test_per_dispense_limit_exceeded(self) -> None:
        rules = [make_rule(7, per_dispense=100, per_30_days=500)]

        [violation] = evaluate(Candidate(NOON, 150), rules=rules).violations

        assert violation.code == MAX_QUANTITY_PER_DISPENSE_EXCEEDED
        assert violation.field == "quantity"
        assert violation.context == {"limit": 100, "requested": 150, "rule_id": 7}
        assert violation.message == "Quantity 150 exceeds the maximum of 100 units per dispense."

    def test_30_day_limit_exceeded(self) -> None:
        rules = [make_rule(4, per_dispense=100, per_30_days=100)]
        history = [prior(1, utc(2026, 3, 1, 10), 60)]

        [violation] = evaluate(Candidate(NOON, 50), rules=rules, history=history).violations

        assert (
            violation.code
            == MAX_QUANTITY_PER_30_DAYS_EXCEEDED
            == ("max_quantity_per_30_days_exceeded")
        )
        assert violation.field == "quantity"
        assert violation.context == {
            "limit": 100,
            "already_dispensed": 60,
            "requested": 50,
            "window_start": "2026-02-14",
            "window_end": "2026-03-15",
            "rule_id": 4,
        }
        for number in ("50", "60", "100", "110"):
            assert number in violation.message

    @pytest.mark.parametrize("blank", [None, "", "   ", "\t\n "])
    def test_missing_or_blank_authorisation_when_required(self, blank: str | None) -> None:
        rules = [make_rule(3, requires_authorisation=True)]

        evaluation = evaluate(Candidate(NOON, 10, blank), rules=rules)

        [violation] = evaluation.violations
        assert violation.code == AUTHORISATION_REQUIRED == "authorisation_required"
        assert violation.field == "authorisation_ref"
        assert violation.context == {"rule_id": 3}

    def test_authorisation_present_when_required(self) -> None:
        rules = [make_rule(requires_authorisation=True)]

        assert evaluate(Candidate(NOON, 10, "AUTH-123"), rules=rules).accepted

    @pytest.mark.parametrize("ref", [None, "", "   ", "AUTH-123"])
    def test_authorisation_is_not_checked_when_not_required(self, ref: str | None) -> None:
        assert evaluate(Candidate(NOON, 10, ref)).accepted


class TestSeveralViolationsAtOnce:
    def test_every_violated_rule_is_reported_in_the_fixed_order(self) -> None:
        rules = [make_rule(9, per_dispense=50, per_30_days=100, requires_authorisation=True)]
        history = [
            prior(1, utc(2026, 3, 10, 10), 40),
            prior(2, utc(2026, 3, 20, 10), 50),
        ]

        evaluation = evaluate(
            Candidate(NOON, 70), rules=rules, history=history, medicine_active=False
        )

        assert codes(evaluation) == [
            MEDICINE_INACTIVE,
            MAX_QUANTITY_PER_DISPENSE_EXCEEDED,
            MAX_QUANTITY_PER_30_DAYS_EXCEEDED,
            AUTHORISATION_REQUIRED,
            LATER_DISPENSE_LIMIT_EXCEEDED,
        ]
        assert [violation.field for violation in evaluation.violations] == [
            "medicine_code",
            "quantity",
            "quantity",
            "authorisation_ref",
            "quantity",
        ]
        assert evaluation.rule == rules[0]

    def test_over_quantity_and_over_the_30_day_limit_are_both_reported(self) -> None:
        rules = [make_rule(per_dispense=100, per_30_days=120)]
        history = [prior(1, utc(2026, 3, 1, 10), 30)]

        evaluation = evaluate(Candidate(NOON, 150), rules=rules, history=history)

        assert codes(evaluation) == [
            MAX_QUANTITY_PER_DISPENSE_EXCEEDED,
            MAX_QUANTITY_PER_30_DAYS_EXCEEDED,
        ]

    def test_inactive_medicine_with_an_over_quantity_request_reports_both(self) -> None:
        rules = [make_rule(per_dispense=100, per_30_days=500)]

        evaluation = evaluate(Candidate(NOON, 150), rules=rules, medicine_active=False)

        assert codes(evaluation) == [MEDICINE_INACTIVE, MAX_QUANTITY_PER_DISPENSE_EXCEEDED]

    def test_inactive_medicine_without_a_rule_reports_only_those_two(self) -> None:
        rules = [make_rule(1, date(2026, 6, 1), per_dispense=1, requires_authorisation=True)]
        history = [prior(1, utc(2026, 3, 1, 10), 1000)]

        evaluation = evaluate(
            Candidate(NOON, 5000), rules=rules, history=history, medicine_active=False
        )

        assert codes(evaluation) == [MEDICINE_INACTIVE, NO_RULE_IN_FORCE]
        assert evaluation.rule is None

    def test_a_missing_rule_does_not_hide_behind_other_problems(self) -> None:
        evaluation = evaluate(Candidate(NOON, 5000), rules=[])

        assert codes(evaluation) == [NO_RULE_IN_FORCE]


class TestWindow:
    RULES = (make_rule(per_dispense=100, per_30_days=100),)
    JAN_1 = (prior(1, utc(2026, 1, 1, 10), 60),)

    @pytest.mark.parametrize(
        ("days_after", "counted"),
        [
            pytest.param(0, True, id="same day"),
            pytest.param(1, True, id="next day"),
            pytest.param(28, True, id="day 29 of the window"),
            pytest.param(29, True, id="day 30 of the window: the last one inside"),
            pytest.param(30, False, id="day 31: the first one outside"),
            pytest.param(31, False, id="day 32"),
        ],
    )
    def test_a_dispense_counts_for_the_30_business_days_it_starts_and_not_the_31st(
        self, days_after: int, counted: bool
    ) -> None:
        candidate = Candidate(utc(2026, 1, 1, 10) + timedelta(days=days_after), 50)

        evaluation = evaluate(candidate, rules=self.RULES, history=self.JAN_1)

        assert codes(evaluation) == ([MAX_QUANTITY_PER_30_DAYS_EXCEEDED] if counted else [])

    def test_the_window_ends_on_the_candidate_date_so_30_january_sees_1_january_and_31_does_not(
        self,
    ) -> None:
        on_30th = evaluate(
            Candidate(utc(2026, 1, 30, 10), 50), rules=self.RULES, history=self.JAN_1
        )
        on_31st = evaluate(
            Candidate(utc(2026, 1, 31, 10), 50), rules=self.RULES, history=self.JAN_1
        )

        [violation] = on_30th.violations
        assert violation.context["window_start"] == "2026-01-01"
        assert violation.context["window_end"] == "2026-01-30"
        assert violation.context["already_dispensed"] == 60
        assert on_31st.accepted

    def test_window_membership_follows_the_sast_business_date_not_the_utc_date(self) -> None:
        # 22:30Z on 31 Dec is 00:30 SAST on 1 Jan: a UTC-date implementation would drop it.
        history = [prior(1, utc(2025, 12, 31, 22, 30), 60)]

        inside = evaluate(
            Candidate(utc(2026, 1, 30, 21, 30), 50), rules=self.RULES, history=history
        )
        outside = evaluate(Candidate(utc(2026, 1, 31, 0, 0), 50), rules=self.RULES, history=history)

        assert codes(inside) == [MAX_QUANTITY_PER_30_DAYS_EXCEEDED]
        assert inside.violations[0].context["already_dispensed"] == 60
        assert outside.accepted

    @pytest.mark.parametrize(
        ("prior_at", "candidate_at"),
        [
            pytest.param(
                utc(2026, 3, 10, 15), utc(2026, 3, 10, 7), id="recorded one is later in the day"
            ),
            pytest.param(
                utc(2026, 3, 10, 7), utc(2026, 3, 10, 15), id="recorded one is earlier in the day"
            ),
            pytest.param(
                utc(2026, 3, 10, 21, 59, 59),
                utc(2026, 3, 9, 22, 0),
                id="opposite ends of the SAST day",
            ),
        ],
    )
    def test_dispenses_on_the_same_business_day_count_whatever_the_time_of_day(
        self, prior_at: datetime, candidate_at: datetime
    ) -> None:
        history = [prior(1, prior_at, 50)]

        evaluation = evaluate(Candidate(candidate_at, 60), rules=self.RULES, history=history)

        assert codes(evaluation) == [MAX_QUANTITY_PER_30_DAYS_EXCEEDED]

    def test_dispenses_on_the_same_business_day_are_summed(self) -> None:
        history = [prior(1, utc(2026, 3, 15, 7), 30), prior(2, utc(2026, 3, 15, 12), 40)]

        [violation] = evaluate(Candidate(NOON, 40), rules=self.RULES, history=history).violations

        assert violation.context["already_dispensed"] == 70

    def test_the_window_is_anchored_on_the_dispensed_at_of_the_candidate_not_on_any_current_time(
        self,
    ) -> None:
        rules = [make_rule(1, date(2020, 1, 1), per_30_days=100)]
        history = [
            prior(1, utc(2025, 6, 1, 10), 80),
            prior(2, utc(2026, 3, 1, 10), 80),
        ]

        evaluation = evaluate(Candidate(utc(2025, 6, 15, 10), 30), rules=rules, history=history)

        [violation] = evaluation.violations
        assert violation.context["already_dispensed"] == 80
        assert violation.context["window_start"] == "2025-05-17"

    def test_the_order_of_the_history_does_not_matter(self) -> None:
        history = [
            prior(1, utc(2026, 3, 1, 10), 20),
            prior(2, utc(2026, 3, 5, 10), 30),
            prior(3, utc(2026, 3, 20, 10), 25),
            prior(4, utc(2026, 3, 25, 10), 20),
            prior(5, utc(2026, 1, 1, 10), 99),
        ]
        candidate = Candidate(utc(2026, 3, 10, 10), 60)
        expected = evaluate(candidate, history=history)
        shuffler = random.Random(20260901)

        for _ in range(10):
            shuffled = history[:]
            shuffler.shuffle(shuffled)
            assert evaluate(candidate, history=shuffled) == expected
        assert codes(expected) == [MAX_QUANTITY_PER_30_DAYS_EXCEEDED, LATER_DISPENSE_LIMIT_EXCEEDED]

    def test_history_just_outside_the_loaded_bounds_cannot_influence_the_result(self) -> None:
        rules = [make_rule(per_dispense=1000, per_30_days=100)]
        lower, upper = history_bounds_utc(date(2026, 3, 15))
        too_old = prior(1, lower - timedelta(microseconds=1), 100)
        too_new = prior(2, upper, 100)

        assert evaluate(Candidate(NOON, 50), rules=rules, history=[too_old, too_new]).accepted

    def test_history_exactly_at_the_loaded_bounds_edges_is_considered(self) -> None:
        rules = [make_rule(per_dispense=1000, per_30_days=100)]
        lower, upper = history_bounds_utc(date(2026, 3, 15))
        oldest = prior(1, lower, 60)
        newest = prior(2, upper - timedelta(microseconds=1), 60)

        own_window = evaluate(Candidate(NOON, 50), rules=rules, history=[oldest])
        later = evaluate(Candidate(NOON, 50), rules=rules, history=[newest])

        assert codes(own_window) == [MAX_QUANTITY_PER_30_DAYS_EXCEEDED]
        assert codes(later) == [LATER_DISPENSE_LIMIT_EXCEEDED]

    def test_extremes_of_the_accepted_input_range_can_be_evaluated(self) -> None:
        rules = [make_rule(1, date(1990, 1, 1))]
        earliest = utc(1990, 1, 1)
        latest = utc(2100, 12, 31, 23, 59, 59)

        assert evaluate(
            Candidate(earliest, 10), rules=rules, history=[prior(1, earliest, 10)]
        ).accepted
        assert evaluate(Candidate(latest, 10), rules=rules, history=[prior(1, latest, 10)]).accepted


class TestBackdating:
    def test_a_backdated_dispense_counts_everything_dated_inside_its_window(self) -> None:
        # Both rows are dated before the candidate but were (in real life) recorded after its
        # date; the engine only sees dates, so recording order cannot matter.
        history = [prior(12, utc(2026, 1, 15, 10), 30), prior(11, utc(2026, 1, 10, 10), 40)]

        evaluation = evaluate(Candidate(utc(2026, 1, 20, 10), 40), history=history)

        [violation] = evaluation.violations
        assert violation.code == MAX_QUANTITY_PER_30_DAYS_EXCEEDED
        assert violation.context["already_dispensed"] == 70
        assert violation.context["window_start"] == "2025-12-22"
        assert violation.context["window_end"] == "2026-01-20"

    def test_a_backdated_dispense_cannot_push_a_recorded_later_dispense_over_its_cap(self) -> None:
        history = [prior(5, utc(2026, 3, 10, 8), 60)]

        evaluation = evaluate(Candidate(utc(2026, 3, 5, 8), 50), history=history)

        [violation] = evaluation.violations
        assert violation.code == LATER_DISPENSE_LIMIT_EXCEEDED == "later_dispense_limit_exceeded"
        assert violation.field == "quantity"
        assert violation.context == {
            "affected": [
                {
                    "business_date": "2026-03-10",
                    "limit": 100,
                    "resulting_total": 110,
                    "dispense_ids": [5],
                }
            ]
        }
        assert "2026-03-10" in violation.message
        assert "110" in violation.message
        assert MAX_QUANTITY_PER_30_DAYS_EXCEEDED not in codes(evaluation)

    def test_a_dispense_on_the_same_business_day_but_later_is_part_of_the_own_window_check(
        self,
    ) -> None:
        history = [prior(5, utc(2026, 3, 10, 15), 50)]

        evaluation = evaluate(Candidate(utc(2026, 3, 10, 7), 60), history=history)

        assert codes(evaluation) == [MAX_QUANTITY_PER_30_DAYS_EXCEEDED]

    def test_each_affected_date_is_checked_against_the_rule_in_force_on_that_date(self) -> None:
        rules = [
            make_rule(1, date(2026, 1, 1), date(2026, 3, 1), per_30_days=100),
            make_rule(2, date(2026, 3, 1), per_30_days=50),
        ]
        history = [prior(21, utc(2026, 2, 20, 10), 30), prior(22, utc(2026, 3, 5, 10), 15)]

        evaluation = evaluate(Candidate(utc(2026, 2, 25, 10), 10), rules=rules, history=history)

        [violation] = evaluation.violations
        assert violation.code == LATER_DISPENSE_LIMIT_EXCEEDED
        assert violation.context["affected"] == [
            {
                "business_date": "2026-03-05",
                "limit": 50,
                "resulting_total": 55,
                "dispense_ids": [22],
            }
        ]
        assert evaluation.rule == rules[0]

    def test_the_own_window_uses_the_cap_of_the_candidates_own_date_not_a_later_rule(self) -> None:
        rules = [
            make_rule(1, date(2026, 1, 1), date(2026, 3, 1), per_30_days=100),
            make_rule(2, date(2026, 3, 1), per_30_days=50),
        ]
        history = [prior(1, utc(2026, 2, 1, 10), 10)]

        assert evaluate(Candidate(utc(2026, 2, 20, 10), 80), rules=rules, history=history).accepted

    def test_every_offending_later_date_is_listed_in_date_order_with_its_own_total_and_ids(
        self,
    ) -> None:
        history = [
            prior(4, utc(2026, 3, 20, 10), 5),
            prior(3, utc(2026, 3, 12, 14), 10),
            prior(1, utc(2026, 3, 10, 10), 40),
            prior(2, utc(2026, 3, 12, 9), 40),
        ]

        evaluation = evaluate(Candidate(utc(2026, 3, 5, 10), 30), history=history)

        [violation] = evaluation.violations
        assert violation.context["affected"] == [
            {
                "business_date": "2026-03-12",
                "limit": 100,
                "resulting_total": 120,
                "dispense_ids": [2, 3],
            },
            {
                "business_date": "2026-03-20",
                "limit": 100,
                "resulting_total": 125,
                "dispense_ids": [4],
            },
        ]

    def test_offending_dates_are_listed_in_ascending_order_whatever_the_history_order(self) -> None:
        offsets_and_ids = [(20, 4), (3, 1), (27, 5), (14, 3), (9, 2)]
        candidate_at = utc(2026, 3, 1, 10)
        history = [
            prior(dispense_id, candidate_at + timedelta(days=offset), 18)
            for offset, dispense_id in offsets_and_ids
        ]

        evaluation = evaluate(Candidate(candidate_at, 90), history=history)

        [violation] = evaluation.violations
        assert violation.code == LATER_DISPENSE_LIMIT_EXCEEDED
        assert [item["business_date"] for item in violation.context["affected"]] == [
            "2026-03-04",
            "2026-03-10",
            "2026-03-15",
            "2026-03-21",
            "2026-03-28",
        ]
        assert [item["resulting_total"] for item in violation.context["affected"]] == [
            108,
            126,
            144,
            162,
            180,
        ]
        assert [item["dispense_ids"] for item in violation.context["affected"]] == [
            [1],
            [2],
            [3],
            [4],
            [5],
        ]

    def test_a_later_dispense_may_be_brought_exactly_to_its_cap_but_not_beyond(self) -> None:
        history = [prior(1, utc(2026, 3, 10, 10), 60)]

        assert evaluate(Candidate(utc(2026, 3, 5, 10), 40), history=history).accepted
        assert codes(evaluate(Candidate(utc(2026, 3, 5, 10), 41), history=history)) == [
            LATER_DISPENSE_LIMIT_EXCEEDED
        ]

    @pytest.mark.parametrize(
        ("days_later", "affected"),
        [
            pytest.param(1, True, id="next day"),
            pytest.param(
                29, True, id="29 days later: this dispense is the oldest day of its window"
            ),
            pytest.param(30, False, id="30 days later: the new dispense is outside its window"),
        ],
    )
    def test_only_recorded_dispenses_within_29_days_after_the_candidate_are_affected(
        self, days_later: int, affected: bool
    ) -> None:
        candidate_at = utc(2026, 3, 5, 10)
        history = [prior(1, candidate_at + timedelta(days=days_later), 100)]

        evaluation = evaluate(Candidate(candidate_at, 1), history=history)

        assert codes(evaluation) == ([LATER_DISPENSE_LIMIT_EXCEEDED] if affected else [])

    def test_later_dispenses_on_a_date_without_a_rule_are_not_checked(self) -> None:
        rules = [make_rule(1, date(2026, 1, 1), date(2026, 3, 1))]
        history = [prior(1, utc(2026, 3, 5, 10), 100)]

        evaluation = evaluate(Candidate(utc(2026, 2, 25, 10), 50), rules=rules, history=history)

        assert evaluation.accepted


class TestInputs:
    def test_naive_candidate_time_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            evaluate(Candidate(datetime(2026, 3, 15, 10), 10))

    def test_overlapping_rules_are_corrupt_data_and_raise_instead_of_picking_one(self) -> None:
        rules = [make_rule(1, date(2026, 1, 1), date(2026, 4, 1)), make_rule(2, date(2026, 2, 1))]

        with pytest.raises(ValueError, match="overlap"):
            evaluate(Candidate(NOON, 10), rules=rules)

    def test_naive_history_time_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            evaluate(Candidate(NOON, 10), history=[prior(1, datetime(2026, 3, 10, 10), 10)])


class TestViolationCatalogue:
    def test_codes_are_the_stable_strings_of_the_error_contract(self) -> None:
        assert [
            MEDICINE_NOT_FOUND,
            MEDICINE_INACTIVE,
            NO_RULE_IN_FORCE,
            MAX_QUANTITY_PER_DISPENSE_EXCEEDED,
            MAX_QUANTITY_PER_30_DAYS_EXCEEDED,
            AUTHORISATION_REQUIRED,
            LATER_DISPENSE_LIMIT_EXCEEDED,
        ] == [
            "medicine_not_found",
            "medicine_inactive",
            "no_rule_in_force",
            "max_quantity_per_dispense_exceeded",
            "max_quantity_per_30_days_exceeded",
            "authorisation_required",
            "later_dispense_limit_exceeded",
        ]

    def test_medicine_not_found_names_the_unknown_code(self) -> None:
        violation = medicine_not_found("MED-9999")

        assert violation.code == "medicine_not_found"
        assert violation.field == "medicine_code"
        assert violation.context == {"medicine_code": "MED-9999"}
        assert "MED-9999" in violation.message
