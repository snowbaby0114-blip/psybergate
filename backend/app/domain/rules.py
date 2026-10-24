"""Formulary rule periods: which rule governs a business date, and how a new rule supersedes.

A period is the half-open date range `[effective_from, effective_to)`. `effective_to` is the first
business day the rule no longer applies; `None` means open-ended.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

RULE_ALREADY_STARTS_ON_DATE = "rule_already_starts_on_date"

RuleStatus = Literal["expired", "in_force", "scheduled"]


@dataclass(frozen=True)
class RuleVersion:
    id: int
    effective_from: date
    effective_to: date | None
    max_quantity_per_dispense: int
    max_quantity_per_30_days: int
    requires_authorisation: bool

    def __post_init__(self) -> None:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError(
                f"rule {self.id}: effective_to {self.effective_to.isoformat()} must be after "
                f"effective_from {self.effective_from.isoformat()}"
            )


@dataclass(frozen=True)
class SupersedePlan:
    truncate_rule_id: int | None
    new_effective_to: date | None


class SupersedeConflict(Exception):
    def __init__(self, code: str, message: str, context: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context


def _covers(rule: RuleVersion, d: date) -> bool:
    return rule.effective_from <= d and (rule.effective_to is None or d < rule.effective_to)


def rule_in_force(rules: Iterable[RuleVersion], d: date) -> RuleVersion | None:
    covering = [rule for rule in rules if _covers(rule, d)]
    if len(covering) > 1:
        ids = ", ".join(str(rule.id) for rule in covering)
        raise ValueError(f"rules {ids} overlap on {d.isoformat()}")
    return covering[0] if covering else None


def rule_status(rule: RuleVersion, today: date) -> RuleStatus:
    if _covers(rule, today):
        return "in_force"
    return "scheduled" if today < rule.effective_from else "expired"


def plan_supersede(rules: Sequence[RuleVersion], effective_from: date) -> SupersedePlan:
    """Plan how a rule starting on `effective_from` fits into the existing timeline.

    Rules are never deleted or moved. A rule that already starts on that date would be erased, so
    that is a conflict. A rule in force on the date is cut short there and the new rule inherits its
    old end. Otherwise nothing is cut and the new rule runs until the next rule starts, or forever.
    """
    for rule in rules:
        if rule.effective_from == effective_from:
            raise SupersedeConflict(
                code=RULE_ALREADY_STARTS_ON_DATE,
                message=(
                    f"Rule {rule.id} already starts on {effective_from.isoformat()}; rules are "
                    "never rewritten or deleted, so choose a different effective_from."
                ),
                context={"rule_id": rule.id, "effective_from": effective_from.isoformat()},
            )

    in_force = rule_in_force(rules, effective_from)
    if in_force is not None:
        return SupersedePlan(truncate_rule_id=in_force.id, new_effective_to=in_force.effective_to)

    next_start = min(
        (rule.effective_from for rule in rules if rule.effective_from > effective_from),
        default=None,
    )
    return SupersedePlan(truncate_rule_id=None, new_effective_to=next_start)
