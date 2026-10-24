"""Pure dispense validation: a candidate dispense against the formulary rules and the recorded
dispenses of one patient and one medicine. No I/O; the caller loads the data under its locks.

Window arithmetic is done on SAST business dates. The window of a dispense on business date `d` is
`[d-29, d]`, and a recorded dispense counts by the date it occurred on, never by when it was
recorded, so the result does not depend on insertion order.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, TypedDict

from app.domain.rules import RuleVersion, rule_in_force
from app.domain.timeutil import WINDOW_DAYS, to_local_date, window_start_date

MEDICINE_NOT_FOUND = "medicine_not_found"
MEDICINE_INACTIVE = "medicine_inactive"
NO_RULE_IN_FORCE = "no_rule_in_force"
MAX_QUANTITY_PER_DISPENSE_EXCEEDED = "max_quantity_per_dispense_exceeded"
MAX_QUANTITY_PER_30_DAYS_EXCEEDED = "max_quantity_per_30_days_exceeded"
AUTHORISATION_REQUIRED = "authorisation_required"
LATER_DISPENSE_LIMIT_EXCEEDED = "later_dispense_limit_exceeded"


@dataclass(frozen=True)
class Candidate:
    dispensed_at: datetime
    quantity: int
    authorisation_ref: str | None = None


@dataclass(frozen=True)
class PriorDispense:
    id: int
    dispensed_at: datetime
    quantity: int


@dataclass(frozen=True)
class Violation:
    code: str
    field: str | None
    message: str
    context: dict[str, Any]


@dataclass(frozen=True)
class Evaluation:
    violations: tuple[Violation, ...]
    rule: RuleVersion | None

    @property
    def accepted(self) -> bool:
        return not self.violations


class AffectedDate(TypedDict):
    business_date: str
    limit: int
    resulting_total: int
    dispense_ids: list[int]


# A recorded dispense with the SAST business date it occurred on.
_DatedPrior = tuple[date, PriorDispense]


def medicine_not_found(code: str) -> Violation:
    return Violation(
        code=MEDICINE_NOT_FOUND,
        field="medicine_code",
        message=f"No medicine with code {code!r} exists.",
        context={"medicine_code": code},
    )


def medicine_inactive() -> Violation:
    return Violation(
        code=MEDICINE_INACTIVE,
        field="medicine_code",
        message="The medicine is inactive and cannot be dispensed.",
        context={},
    )


def no_rule_in_force(business_date: date) -> Violation:
    return Violation(
        code=NO_RULE_IN_FORCE,
        field="dispensed_at",
        message=f"No formulary rule is in force on {business_date.isoformat()} (SAST).",
        context={"business_date": business_date.isoformat()},
    )


def max_quantity_per_dispense_exceeded(rule: RuleVersion, requested: int) -> Violation:
    return Violation(
        code=MAX_QUANTITY_PER_DISPENSE_EXCEEDED,
        field="quantity",
        message=(
            f"Quantity {requested} exceeds the maximum of "
            f"{rule.max_quantity_per_dispense} units per dispense."
        ),
        context={
            "limit": rule.max_quantity_per_dispense,
            "requested": requested,
            "rule_id": rule.id,
        },
    )


def max_quantity_per_30_days_exceeded(
    rule: RuleVersion,
    *,
    already_dispensed: int,
    requested: int,
    window_start: date,
    window_end: date,
) -> Violation:
    return Violation(
        code=MAX_QUANTITY_PER_30_DAYS_EXCEEDED,
        field="quantity",
        message=(
            f"Quantity {requested} would bring the {WINDOW_DAYS}-day total to "
            f"{already_dispensed + requested}, above the maximum of "
            f"{rule.max_quantity_per_30_days} units ({already_dispensed} already dispensed "
            f"from {window_start.isoformat()} to {window_end.isoformat()})."
        ),
        context={
            "limit": rule.max_quantity_per_30_days,
            "already_dispensed": already_dispensed,
            "requested": requested,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "rule_id": rule.id,
        },
    )


def authorisation_required(rule: RuleVersion) -> Violation:
    return Violation(
        code=AUTHORISATION_REQUIRED,
        field="authorisation_ref",
        message="An authorisation reference is required to dispense this medicine.",
        context={"rule_id": rule.id},
    )


def later_dispense_limit_exceeded(affected: Sequence[AffectedDate]) -> Violation:
    totals = "; ".join(
        f"{item['business_date']} would total {item['resulting_total']} "
        f"against a limit of {item['limit']}"
        for item in affected
    )
    return Violation(
        code=LATER_DISPENSE_LIMIT_EXCEEDED,
        field="quantity",
        message=(
            f"Recording this dispense would push later dispenses over their {WINDOW_DAYS}-day "
            f"limit: {totals}."
        ),
        context={"affected": list(affected)},
    )


def evaluate_dispense(
    *,
    medicine_active: bool,
    rules: Sequence[RuleVersion],
    history: Sequence[PriorDispense],
    candidate: Candidate,
) -> Evaluation:
    """Return every violated rule, in the fixed order of the catalogue.

    `history` may hold dispenses of any date: only those dated within 29 days either side of the
    candidate can influence the result, so the caller may load just `history_bounds_utc`.
    """
    business_date = to_local_date(candidate.dispensed_at)
    rule = rule_in_force(rules, business_date)

    violations: list[Violation] = []
    if not medicine_active:
        violations.append(medicine_inactive())
    if rule is None:
        violations.append(no_rule_in_force(business_date))
        return Evaluation(violations=tuple(violations), rule=None)

    if candidate.quantity > rule.max_quantity_per_dispense:
        violations.append(max_quantity_per_dispense_exceeded(rule, candidate.quantity))

    recorded = [(to_local_date(prior.dispensed_at), prior) for prior in history]
    already_dispensed = _window_total(recorded, business_date)
    if already_dispensed + candidate.quantity > rule.max_quantity_per_30_days:
        violations.append(
            max_quantity_per_30_days_exceeded(
                rule,
                already_dispensed=already_dispensed,
                requested=candidate.quantity,
                window_start=window_start_date(business_date),
                window_end=business_date,
            )
        )

    if rule.requires_authorisation and _is_blank(candidate.authorisation_ref):
        violations.append(authorisation_required(rule))

    affected = _later_dispenses_pushed_over_their_cap(
        rules, recorded, business_date, candidate.quantity
    )
    if affected:
        violations.append(later_dispense_limit_exceeded(affected))

    return Evaluation(violations=tuple(violations), rule=rule)


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _window_total(recorded: Sequence[_DatedPrior], day: date) -> int:
    first = window_start_date(day)
    return sum(prior.quantity for occurred_on, prior in recorded if first <= occurred_on <= day)


def _later_dispenses_pushed_over_their_cap(
    rules: Sequence[RuleVersion],
    recorded: Sequence[_DatedPrior],
    business_date: date,
    quantity: int,
) -> list[AffectedDate]:
    """The new dispense sits inside the window of every recorded dispense dated in
    `(business_date, business_date + 29]`, so each of those dates must still fit its own cap."""
    last_affected = business_date + timedelta(days=WINDOW_DAYS - 1)
    later_dates = sorted(
        {occurred_on for occurred_on, _ in recorded if business_date < occurred_on <= last_affected}
    )
    affected: list[AffectedDate] = []
    for day in later_dates:
        rule = rule_in_force(rules, day)
        if rule is None:
            continue
        resulting_total = _window_total(recorded, day) + quantity
        if resulting_total > rule.max_quantity_per_30_days:
            affected.append(
                AffectedDate(
                    business_date=day.isoformat(),
                    limit=rule.max_quantity_per_30_days,
                    resulting_total=resulting_total,
                    dispense_ids=sorted(
                        prior.id for occurred_on, prior in recorded if occurred_on == day
                    ),
                )
            )
    return affected
