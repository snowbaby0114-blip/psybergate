from dataclasses import replace
from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.rules import (
    RULE_ALREADY_STARTS_ON_DATE,
    RuleVersion,
    SupersedeConflict,
    SupersedePlan,
    plan_supersede,
    rule_in_force,
    rule_status,
)

NEW_RULE_ID = 999


def make_rule(rule_id: int, start: date, end: date | None = None) -> RuleVersion:
    return RuleVersion(
        id=rule_id,
        effective_from=start,
        effective_to=end,
        max_quantity_per_dispense=10,
        max_quantity_per_30_days=30,
        requires_authorisation=False,
    )


def covering_ids(rules: list[RuleVersion], day: date) -> list[int]:
    """Oracle: ids of the rules whose period contains `day`, found without any engine code."""
    return [
        rule.id
        for rule in rules
        if rule.effective_from <= day and (rule.effective_to is None or day < rule.effective_to)
    ]


def paint(rules: list[RuleVersion], horizon: date) -> dict[date, int]:
    """Oracle: the governing rule id of every covered day before `horizon`, day by day."""
    painted: dict[date, int] = {}
    for rule in rules:
        for offset in range(((rule.effective_to or horizon) - rule.effective_from).days):
            day = rule.effective_from + timedelta(days=offset)
            assert day not in painted, "test data must not overlap"
            painted[day] = rule.id
    return painted


def apply_plan(rules: list[RuleVersion], plan: SupersedePlan, d: date) -> list[RuleVersion]:
    truncated = [
        replace(rule, effective_to=d) if rule.id == plan.truncate_rule_id else rule
        for rule in rules
    ]
    return [*truncated, make_rule(NEW_RULE_ID, d, plan.new_effective_to)]


@st.composite
def tilings(draw: st.DrawFn) -> tuple[list[RuleVersion], date]:
    """Gap-free, overlap-free timelines in shuffled order, and a horizon after all of them."""
    cursor = draw(st.dates(min_value=date(1990, 1, 1), max_value=date(2099, 1, 1)))
    lengths = draw(st.lists(st.integers(min_value=1, max_value=400), min_size=1, max_size=6))
    open_ended = draw(st.booleans())
    rules: list[RuleVersion] = []
    for index, length in enumerate(lengths):
        is_last = index == len(lengths) - 1
        end = None if is_last and open_ended else cursor + timedelta(days=length)
        rules.append(make_rule(index + 1, cursor, end))
        if end is not None:
            cursor = end
    return draw(st.permutations(rules)), cursor + timedelta(days=400)


@st.composite
def timelines_with_a_start_date(draw: st.DrawFn) -> tuple[list[RuleVersion], date]:
    """Overlap-free timelines that may have gaps, in shuffled order, and a candidate start date
    that is often right at or next to an existing boundary."""
    cursor = draw(st.dates(min_value=date(2020, 1, 1), max_value=date(2022, 12, 31)))
    count = draw(st.integers(min_value=0, max_value=5))
    rules: list[RuleVersion] = []
    for index in range(count):
        cursor += timedelta(days=draw(st.integers(min_value=0, max_value=40)))
        open_ended = index == count - 1 and draw(st.booleans())
        end = None if open_ended else cursor + timedelta(days=draw(st.integers(1, 120)))
        rules.append(make_rule(index + 1, cursor, end))
        if end is not None:
            cursor = end
    near_boundaries = sorted(
        {
            boundary + timedelta(days=shift)
            for rule in rules
            for boundary in (rule.effective_from, rule.effective_to)
            if boundary is not None
            for shift in (-1, 0, 1)
        }
    )
    anywhere = st.dates(min_value=date(2019, 12, 1), max_value=date(2023, 12, 31))
    d = draw(st.one_of(anywhere, st.sampled_from(near_boundaries)) if near_boundaries else anywhere)
    return draw(st.permutations(rules)), d


TILED = [make_rule(1, date(2026, 1, 1), date(2026, 3, 1)), make_rule(2, date(2026, 3, 1))]
GAPPED = [
    make_rule(1, date(2026, 1, 1), date(2026, 2, 1)),
    make_rule(2, date(2026, 3, 1), date(2026, 4, 1)),
]


class TestRuleVersion:
    @pytest.mark.parametrize(
        "end", [date(2026, 3, 1), date(2026, 2, 28)], ids=["empty", "inverted"]
    )
    def test_rejects_a_period_that_is_empty_or_ends_before_it_starts(self, end: date) -> None:
        with pytest.raises(ValueError, match="effective_to"):
            make_rule(1, date(2026, 3, 1), end)

    def test_accepts_closed_and_open_ended_periods(self) -> None:
        assert make_rule(1, date(2026, 3, 1), date(2026, 3, 2)).effective_to == date(2026, 3, 2)
        assert make_rule(2, date(2026, 3, 1)).effective_to is None


class TestRuleInForce:
    @pytest.mark.parametrize(
        ("rules", "day", "expected_id"),
        [
            pytest.param(TILED, date(2025, 12, 31), None, id="tiled: day before the first start"),
            pytest.param(TILED, date(2026, 1, 1), 1, id="tiled: start is inclusive"),
            pytest.param(TILED, date(2026, 2, 28), 1, id="tiled: last day of the first rule"),
            pytest.param(TILED, date(2026, 3, 1), 2, id="tiled: boundary day is the new rule's"),
            pytest.param(TILED, date(2100, 1, 1), 2, id="tiled: open-ended rule runs forever"),
            pytest.param(GAPPED, date(2026, 1, 31), 1, id="gapped: last day of the first rule"),
            pytest.param(GAPPED, date(2026, 2, 1), None, id="gapped: effective_to is exclusive"),
            pytest.param(GAPPED, date(2026, 2, 28), None, id="gapped: inside the gap"),
            pytest.param(GAPPED, date(2026, 3, 1), 2, id="gapped: start of the second rule"),
            pytest.param(GAPPED, date(2026, 3, 31), 2, id="gapped: last day of the second rule"),
            pytest.param(GAPPED, date(2026, 4, 1), None, id="gapped: after the last closed rule"),
            pytest.param([], date(2026, 3, 1), None, id="no rules"),
        ],
    )
    def test_returns_the_rule_whose_half_open_period_contains_the_day(
        self, rules: list[RuleVersion], day: date, expected_id: int | None
    ) -> None:
        found = rule_in_force(rules, day)

        assert (found.id if found else None) == expected_id

    def test_does_not_depend_on_the_order_of_the_rules(self) -> None:
        rules = [
            make_rule(3, date(2026, 6, 1)),
            make_rule(1, date(2026, 1, 1), date(2026, 3, 1)),
            make_rule(2, date(2026, 3, 1), date(2026, 6, 1)),
        ]

        assert rule_in_force(rules, date(2026, 2, 1)) == rules[1]
        assert rule_in_force(rules, date(2026, 3, 1)) == rules[2]
        assert rule_in_force(rules, date(2026, 6, 1)) == rules[0]

    def test_refuses_to_guess_between_overlapping_rules(self) -> None:
        early = make_rule(1, date(2026, 1, 1), date(2026, 3, 1))
        overlapping = make_rule(2, date(2026, 2, 1))

        with pytest.raises(ValueError, match="rules 1, 2 overlap on 2026-02-15"):
            rule_in_force([early, overlapping], date(2026, 2, 15))
        assert rule_in_force([early, overlapping], date(2026, 1, 15)) == early

    @settings(deadline=None)
    @given(st.data())
    def test_a_tiled_timeline_has_exactly_one_rule_per_covered_day_and_none_outside(
        self, data: st.DataObject
    ) -> None:
        rules, horizon = data.draw(tilings())
        painted = paint(rules, horizon)
        first = min(rule.effective_from for rule in rules)
        probe = first + timedelta(days=data.draw(st.integers(-100, (horizon - first).days - 1)))

        found = rule_in_force(rules, probe)

        assert (found.id if found else None) == painted.get(probe)


class TestRuleStatus:
    PERIOD = make_rule(1, date(2026, 3, 1), date(2026, 6, 1))
    OPEN_ENDED = make_rule(2, date(2026, 3, 1))

    @pytest.mark.parametrize(
        ("rule", "today", "expected"),
        [
            pytest.param(PERIOD, date(2026, 2, 28), "scheduled", id="day before start"),
            pytest.param(PERIOD, date(2026, 3, 1), "in_force", id="first day"),
            pytest.param(PERIOD, date(2026, 5, 31), "in_force", id="last day"),
            pytest.param(PERIOD, date(2026, 6, 1), "expired", id="effective_to is exclusive"),
            pytest.param(PERIOD, date(2030, 1, 1), "expired", id="long after"),
            pytest.param(OPEN_ENDED, date(2026, 2, 28), "scheduled", id="open-ended, before"),
            pytest.param(OPEN_ENDED, date(2026, 3, 1), "in_force", id="open-ended, first day"),
            pytest.param(OPEN_ENDED, date(2100, 1, 1), "in_force", id="open-ended, never expires"),
        ],
    )
    def test_is_relative_to_the_business_date_today(
        self, rule: RuleVersion, today: date, expected: str
    ) -> None:
        assert rule_status(rule, today) == expected

    @settings(deadline=None)
    @given(st.data())
    def test_exactly_the_rule_in_force_is_in_force_and_the_others_are_past_or_future(
        self, data: st.DataObject
    ) -> None:
        rules, horizon = data.draw(tilings())
        first = min(rule.effective_from for rule in rules)
        today = first + timedelta(days=data.draw(st.integers(-100, (horizon - first).days - 1)))
        governing_id = paint(rules, horizon).get(today)

        for rule in rules:
            if rule.id == governing_id:
                assert rule_status(rule, today) == "in_force"
            elif today < rule.effective_from:
                assert rule_status(rule, today) == "scheduled"
            else:
                assert rule_status(rule, today) == "expired"


class TestPlanSupersede:
    @pytest.mark.parametrize(
        ("rules", "d", "expected"),
        [
            pytest.param([], date(2026, 3, 1), SupersedePlan(None, None), id="no rules"),
            pytest.param(
                [make_rule(1, date(2026, 1, 1))],
                date(2026, 3, 1),
                SupersedePlan(1, None),
                id="inside an open-ended rule: truncate it, new rule is open-ended",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1))],
                date(2026, 1, 2),
                SupersedePlan(1, None),
                id="the day after a rule starts",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1), date(2026, 6, 1)), make_rule(2, date(2026, 6, 1))],
                date(2026, 3, 1),
                SupersedePlan(1, date(2026, 6, 1)),
                id="inside a closed rule followed by another: inherit its end, leave the next",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1), date(2026, 6, 1)), make_rule(2, date(2026, 8, 1))],
                date(2026, 3, 1),
                SupersedePlan(1, date(2026, 6, 1)),
                id="inside a closed rule followed by a gap: inherit its end, keep the gap",
            ),
            pytest.param(
                [make_rule(2, date(2026, 6, 1)), make_rule(1, date(2026, 1, 1), date(2026, 6, 1))],
                date(2026, 3, 1),
                SupersedePlan(1, date(2026, 6, 1)),
                id="rules given out of order",
            ),
            pytest.param(
                [
                    make_rule(1, date(2026, 1, 1), date(2026, 3, 1)),
                    make_rule(2, date(2026, 3, 1), date(2026, 6, 1)),
                    make_rule(3, date(2026, 6, 1)),
                ],
                date(2026, 4, 15),
                SupersedePlan(2, date(2026, 6, 1)),
                id="inside the middle rule of three: later rules untouched",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1), date(2026, 3, 1)), make_rule(2, date(2026, 3, 1))],
                date(2026, 2, 28),
                SupersedePlan(1, date(2026, 3, 1)),
                id="last day of a rule that is followed by another",
            ),
            pytest.param(
                [make_rule(1, date(2026, 3, 1))],
                date(2026, 1, 1),
                SupersedePlan(None, date(2026, 3, 1)),
                id="before the first rule: end where the first starts",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1), date(2026, 2, 1)), make_rule(2, date(2026, 4, 1))],
                date(2026, 3, 1),
                SupersedePlan(None, date(2026, 4, 1)),
                id="in a gap between rules: fill until the next rule",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1), date(2026, 2, 1))],
                date(2026, 3, 1),
                SupersedePlan(None, None),
                id="after the last closed rule: open-ended",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1), date(2026, 2, 1))],
                date(2026, 2, 1),
                SupersedePlan(None, None),
                id="exactly at a rule's exclusive end, nothing follows",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1), date(2026, 2, 1)), make_rule(2, date(2026, 4, 1))],
                date(2026, 2, 1),
                SupersedePlan(None, date(2026, 4, 1)),
                id="exactly at a rule's exclusive end, a later rule follows after a gap",
            ),
        ],
    )
    def test_plans_truncation_and_end_of_the_new_rule(
        self, rules: list[RuleVersion], d: date, expected: SupersedePlan
    ) -> None:
        assert plan_supersede(rules, d) == expected

    @pytest.mark.parametrize(
        ("rules", "d", "conflicting_rule_id"),
        [
            pytest.param(
                [make_rule(1, date(2026, 1, 1))],
                date(2026, 1, 1),
                1,
                id="start of the in-force rule",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1), date(2026, 2, 1)), make_rule(2, date(2026, 2, 1))],
                date(2026, 2, 1),
                2,
                id="start of the rule that follows adjacent to another",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1), date(2026, 2, 1)), make_rule(2, date(2026, 9, 1))],
                date(2026, 9, 1),
                2,
                id="start of a future rule after a gap",
            ),
            pytest.param(
                [make_rule(1, date(2026, 1, 1), date(2026, 2, 1)), make_rule(2, date(2026, 2, 1))],
                date(2026, 1, 1),
                1,
                id="start of an expired rule",
            ),
        ],
    )
    def test_refuses_a_date_on_which_a_rule_already_starts(
        self, rules: list[RuleVersion], d: date, conflicting_rule_id: int
    ) -> None:
        with pytest.raises(SupersedeConflict) as excinfo:
            plan_supersede(rules, d)

        conflict = excinfo.value
        assert conflict.code == RULE_ALREADY_STARTS_ON_DATE == "rule_already_starts_on_date"
        assert conflict.context == {"rule_id": conflicting_rule_id, "effective_from": d.isoformat()}
        assert d.isoformat() in conflict.message
        assert str(conflict) == conflict.message

    @settings(deadline=None, max_examples=300)
    @given(st.data())
    def test_new_rule_replaces_what_governed_its_first_day_for_as_long_as_that_lasted(
        self, data: st.DataObject
    ) -> None:
        rules, d = data.draw(timelines_with_a_start_date())
        try:
            plan = plan_supersede(rules, d)
        except SupersedeConflict:
            assert d in {rule.effective_from for rule in rules}
            return
        boundaries = [d] + [
            boundary
            for rule in rules
            for boundary in (rule.effective_from, rule.effective_to)
            if boundary is not None
        ]
        first, last = min(boundaries) - timedelta(days=10), max(boundaries) + timedelta(days=10)
        days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
        before = {day: covering_ids(rules, day) for day in days}
        stretch_end = next((day for day in days if day > d and before[day] != before[d]), None)
        updated = apply_plan(rules, plan, d)

        for day in days:
            takes_over = d <= day and (stretch_end is None or day < stretch_end)
            assert covering_ids(updated, day) == ([NEW_RULE_ID] if takes_over else before[day])

    @settings(deadline=None, max_examples=300)
    @given(st.data())
    def test_only_the_end_of_the_rule_in_force_ever_changes(self, data: st.DataObject) -> None:
        rules, d = data.draw(timelines_with_a_start_date())
        try:
            plan = plan_supersede(rules, d)
        except SupersedeConflict:
            return
        updated = {rule.id: rule for rule in apply_plan(rules, plan, d)}

        assert set(updated) == {rule.id for rule in rules} | {NEW_RULE_ID}
        for rule in rules:
            assert replace(updated[rule.id], effective_to=rule.effective_to) == rule
        changed = [rule.id for rule in rules if updated[rule.id].effective_to != rule.effective_to]
        assert changed == ([] if plan.truncate_rule_id is None else [plan.truncate_rule_id])
        assert updated[NEW_RULE_ID].effective_from == d
