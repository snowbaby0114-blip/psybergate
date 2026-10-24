"""The rule history of a medicine, and introducing a new rule that supersedes what is in force."""

from collections.abc import Sequence
from datetime import date

from sqlalchemy.orm import Session

from app import errors, repositories
from app.domain.dispense import Violation
from app.domain.rules import (
    RuleVersion,
    SupersedeConflict,
    SupersedePlan,
    plan_supersede,
    rule_in_force,
    rule_status,
)
from app.domain.timeutil import local_day_start_utc
from app.models import FormularyRule
from app.schemas import RuleCreate, RuleListOut, RuleOut, RuleSupersedeResult


def to_rule_version(row: FormularyRule) -> RuleVersion:
    return RuleVersion(
        id=row.id,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        max_quantity_per_dispense=row.max_quantity_per_dispense,
        max_quantity_per_30_days=row.max_quantity_per_30_days,
        requires_authorisation=row.requires_authorisation,
    )


def rule_in_force_on(rows: Sequence[FormularyRule], day: date) -> FormularyRule | None:
    """The row of the rule in force on a business day, chosen by the same pure function that
    validates dispenses."""
    in_force = rule_in_force([to_rule_version(row) for row in rows], day)
    return None if in_force is None else next(row for row in rows if row.id == in_force.id)


def rule_out(row: FormularyRule, today: date) -> RuleOut:
    return RuleOut(
        id=row.id,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        max_quantity_per_dispense=row.max_quantity_per_dispense,
        max_quantity_per_30_days=row.max_quantity_per_30_days,
        requires_authorisation=row.requires_authorisation,
        status=rule_status(to_rule_version(row), today),
        created_at=row.created_at,
    )


def list_rules(session: Session, code: str, today: date) -> RuleListOut:
    with session.begin():
        medicine = repositories.get_medicine_by_code(session, code)
        if medicine is None:
            raise errors.medicine_not_found(code)
        rows = repositories.list_rules(session, medicine.id)
    return RuleListOut(items=[rule_out(row, today) for row in rows])


def supersede_rule(
    session: Session, code: str, body: RuleCreate, *, today: date, lock_timeout_ms: int
) -> RuleSupersedeResult:
    """Introduce a rule from `body.effective_from` in one transaction.

    The medicine row is locked first, so concurrent changes to its rules, and dispenses (which
    lock the same row FOR SHARE), queue behind this one and see its result. The old rule is
    shortened before the new one is inserted, so the exclusion constraint never sees an overlap.
    """
    with session.begin():
        repositories.set_local_lock_timeout(session, lock_timeout_ms)
        medicine = repositories.get_medicine_by_code(session, code, lock="update")
        if medicine is None:
            raise errors.medicine_not_found(code)
        rows = repositories.list_rules(session, medicine.id)
        plan = _plan(rows, body.effective_from)
        _ensure_recorded_dispenses_keep_their_rule(
            session, medicine.id, body.effective_from, plan.new_effective_to
        )
        shortened = None
        if plan.truncate_rule_id is not None:
            shortened = next(row for row in rows if row.id == plan.truncate_rule_id)
            repositories.end_rule(session, shortened, body.effective_from)
        created = repositories.add_rule(
            session,
            medicine_id=medicine.id,
            effective_from=body.effective_from,
            effective_to=plan.new_effective_to,
            max_quantity_per_dispense=body.max_quantity_per_dispense,
            max_quantity_per_30_days=body.max_quantity_per_30_days,
            requires_authorisation=body.requires_authorisation,
        )
    return RuleSupersedeResult(
        rule=rule_out(created, today),
        superseded=rule_out(shortened, today) if shortened is not None else None,
    )


def _plan(rows: list[FormularyRule], effective_from: date) -> SupersedePlan:
    try:
        return plan_supersede([to_rule_version(row) for row in rows], effective_from)
    except SupersedeConflict as conflict:
        violation = Violation(
            code=conflict.code,
            field="effective_from",
            message=conflict.message,
            context=conflict.context,
        )
        raise errors.rule_conflict(violation) from conflict


def _ensure_recorded_dispenses_keep_their_rule(
    session: Session, medicine_id: int, effective_from: date, new_effective_to: date | None
) -> None:
    """A rule change must not retroactively re-govern a dispense that is already on record.

    The new rule governs business days [effective_from, new_effective_to), so any dispense
    inside that span would silently change which limits it was validated against. Checked under
    the medicine lock, so a dispense cannot be recorded between this check and the insert.
    """
    recorded = repositories.dispense_summary(
        session,
        medicine_id,
        start=local_day_start_utc(effective_from),
        end=None if new_effective_to is None else local_day_start_utc(new_effective_to),
    )
    if recorded is not None:
        raise errors.rule_conflict(
            errors.rule_would_alter_recorded_dispenses(
                effective_from=effective_from,
                count=recorded.count,
                earliest=recorded.earliest,
                latest=recorded.latest,
            )
        )
