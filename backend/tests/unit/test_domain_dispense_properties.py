"""Whatever order dispenses are recorded in, admitting exactly those the engine accepts never leaves
a recorded dispense over its cap.

The oracle below restates the admission invariant of the SPEC with plain date arithmetic on the
business dates the test generated, without any of the engine's or timeutil's window helpers.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone

from hypothesis import find, given, settings
from hypothesis import strategies as st

from app.domain.dispense import Candidate, PriorDispense, evaluate_dispense
from app.domain.rules import RuleVersion
from app.domain.timeutil import history_bounds_utc

BASE = date(2026, 1, 1)
SPAN_DAYS = 90
# Johannesburg has no daylight saving: a fixed offset builds instants independently of zoneinfo.
SAST = timezone(timedelta(hours=2))


@dataclass(frozen=True)
class Attempt:
    business_date: date
    second_of_day: int
    quantity: int

    @property
    def instant(self) -> datetime:
        local = datetime.combine(self.business_date, time.min, tzinfo=SAST)
        return (local + timedelta(seconds=self.second_of_day)).astimezone(UTC)


@dataclass(frozen=True)
class Decision:
    attempt: Attempt
    accepted: bool
    keeps_ledger_valid: bool


Decide = Callable[[Sequence[RuleVersion], Sequence[Attempt], Attempt], bool]


def make_rule(
    rule_id: int, start: date, end: date | None, per_dispense: int, per_30_days: int
) -> RuleVersion:
    return RuleVersion(
        id=rule_id,
        effective_from=start,
        effective_to=end,
        max_quantity_per_dispense=per_dispense,
        max_quantity_per_30_days=per_30_days,
        requires_authorisation=False,
    )


attempts = st.builds(
    Attempt,
    business_date=st.integers(0, SPAN_DAYS - 1).map(lambda n: BASE + timedelta(days=n)),
    second_of_day=st.one_of(st.sampled_from([0, 7199, 7200, 86399]), st.integers(0, 86399)),
    quantity=st.integers(1, 60),
)


@st.composite
def rule_sets(draw: st.DrawFn) -> list[RuleVersion]:
    """One rule, two rules with different caps, or two rules with a gap between them."""

    def limits() -> tuple[int, int]:
        return draw(st.integers(20, 60)), draw(st.integers(40, 150))

    first_start = BASE - timedelta(days=60)
    shape = draw(st.sampled_from(["single", "changing", "gap"]))
    if shape == "single":
        return [make_rule(1, first_start, None, *limits())]
    change = BASE + timedelta(days=draw(st.integers(1, SPAN_DAYS - 1)))
    resume = change if shape == "changing" else change + timedelta(days=draw(st.integers(1, 20)))
    return [
        make_rule(1, first_start, change, *limits()),
        make_rule(2, resume, None, *limits()),
    ]


def rule_covering(rules: Sequence[RuleVersion], day: date) -> RuleVersion | None:
    for rule in rules:
        if rule.effective_from <= day and (rule.effective_to is None or day < rule.effective_to):
            return rule
    return None


def window_sum(ledger: Sequence[Attempt], day: date) -> int:
    return sum(
        recorded.quantity
        for recorded in ledger
        if day - timedelta(days=29) <= recorded.business_date <= day
    )


def ledger_is_valid(rules: Sequence[RuleVersion], ledger: Sequence[Attempt]) -> bool:
    """The admission invariant: every recorded dispense is governed by a rule and respects both
    of its quantity limits, counting everything dated in the 30 business days up to its date."""
    for recorded in ledger:
        rule = rule_covering(rules, recorded.business_date)
        if rule is None or recorded.quantity > rule.max_quantity_per_dispense:
            return False
        if window_sum(ledger, recorded.business_date) > rule.max_quantity_per_30_days:
            return False
    return True


def as_history(ledger: Sequence[Attempt]) -> list[PriorDispense]:
    return [
        PriorDispense(id=index, dispensed_at=recorded.instant, quantity=recorded.quantity)
        for index, recorded in enumerate(ledger)
    ]


def engine_with_all_history(
    rules: Sequence[RuleVersion], ledger: Sequence[Attempt], attempt: Attempt
) -> bool:
    evaluation = evaluate_dispense(
        medicine_active=True,
        rules=rules,
        history=as_history(ledger),
        candidate=Candidate(attempt.instant, attempt.quantity),
    )
    return evaluation.accepted


def engine_with_history_the_service_loads(
    rules: Sequence[RuleVersion], ledger: Sequence[Attempt], attempt: Attempt
) -> bool:
    lower, upper = history_bounds_utc(attempt.business_date)
    loaded = [prior for prior in as_history(ledger) if lower <= prior.dispensed_at < upper]
    evaluation = evaluate_dispense(
        medicine_active=True,
        rules=rules,
        history=loaded,
        candidate=Candidate(attempt.instant, attempt.quantity),
    )
    return evaluation.accepted


def own_window_only(
    rules: Sequence[RuleVersion], ledger: Sequence[Attempt], attempt: Attempt
) -> bool:
    """A plausible but wrong implementation: it never looks at dispenses dated after the new one."""
    rule = rule_covering(rules, attempt.business_date)
    return (
        rule is not None
        and attempt.quantity <= rule.max_quantity_per_dispense
        and window_sum(ledger, attempt.business_date) + attempt.quantity
        <= rule.max_quantity_per_30_days
    )


def record_in_order(
    rules: Sequence[RuleVersion], in_order: Sequence[Attempt], decide: Decide
) -> list[Decision]:
    ledger: list[Attempt] = []
    decisions: list[Decision] = []
    for attempt in in_order:
        accepted = decide(rules, ledger, attempt)
        keeps_ledger_valid = ledger_is_valid(rules, [*ledger, attempt])
        decisions.append(Decision(attempt, accepted, keeps_ledger_valid))
        if accepted:
            ledger.append(attempt)
    return decisions


def accepted_ledger(decisions: Sequence[Decision]) -> list[Attempt]:
    return [decision.attempt for decision in decisions if decision.accepted]


@settings(deadline=None, max_examples=300)
@given(rule_sets(), st.lists(attempts, min_size=8, max_size=40))
def test_every_accepted_dispense_still_fits_its_cap_whatever_the_recording_order(
    rules: list[RuleVersion], in_order: list[Attempt]
) -> None:
    decisions = record_in_order(rules, in_order, engine_with_all_history)

    assert ledger_is_valid(rules, accepted_ledger(decisions))


@settings(deadline=None, max_examples=300)
@given(rule_sets(), st.lists(attempts, min_size=8, max_size=40))
def test_a_dispense_is_accepted_exactly_when_the_ledger_stays_valid_with_it(
    rules: list[RuleVersion], in_order: list[Attempt]
) -> None:
    decisions = record_in_order(rules, in_order, engine_with_all_history)

    assert all(decision.accepted == decision.keeps_ledger_valid for decision in decisions)


@settings(deadline=None, max_examples=300)
@given(rule_sets(), st.lists(attempts, min_size=8, max_size=40))
def test_loading_only_the_history_bounds_the_service_uses_changes_no_decision(
    rules: list[RuleVersion], in_order: list[Attempt]
) -> None:
    everything = record_in_order(rules, in_order, engine_with_all_history)
    bounded = record_in_order(rules, in_order, engine_with_history_the_service_loads)

    assert bounded == everything


def test_the_property_can_fail_a_check_that_ignores_later_dispenses_is_caught() -> None:
    search = st.tuples(rule_sets(), st.lists(attempts, min_size=2, max_size=40))

    rules, in_order = find(
        search,
        lambda case: (
            not ledger_is_valid(
                case[0], accepted_ledger(record_in_order(case[0], case[1], own_window_only))
            )
        ),
        settings=settings(deadline=None, max_examples=2000, database=None),
    )

    assert not ledger_is_valid(
        rules, accepted_ledger(record_in_order(rules, in_order, own_window_only))
    )
    assert ledger_is_valid(
        rules, accepted_ledger(record_in_order(rules, in_order, engine_with_all_history))
    )
