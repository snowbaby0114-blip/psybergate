"""All SQL of the application lives here. Services decide when it runs and own the transaction;
nothing in this module commits.

The query builders return Select objects so that tests and scripts can EXPLAIN exactly the
statement that production runs.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Literal

from sqlalchemy import Row, Select, func, or_, select, text, tuple_
from sqlalchemy.orm import Session

from app.domain.dispense import PriorDispense
from app.models import Dispense, DispenseAttempt, FormularyRule, Medicine

MedicineLock = Literal["share", "update"]
AttemptOutcome = Literal["accepted", "rejected"]
# A dispense with the code and name of its medicine, as a listing and a replay show it.
DispenseRow = Row[tuple[Dispense, str, str]]

# A KeyError on any other value is the point: a misspelt lock must not silently mean "no lock".
_LOCK_IS_SHARED: Final = {"share": True, "update": False}


@dataclass(frozen=True)
class DispenseSummary:
    count: int
    earliest: datetime
    latest: datetime


def _escape_like(term: str) -> str:
    # The backslash first, or the escapes added for % and _ would themselves be escaped.
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_medicine_search_stmt(
    q: str | None, after: tuple[str, str] | None, limit: int
) -> Select[tuple[Medicine]]:
    # limit + 1: the extra row only tells the caller whether another page exists.
    stmt = select(Medicine).order_by(Medicine.name, Medicine.code).limit(limit + 1)
    if q:
        pattern = f"%{_escape_like(q)}%"
        stmt = stmt.where(
            or_(
                Medicine.name.ilike(pattern, escape="\\"),
                Medicine.code.ilike(pattern, escape="\\"),
            )
        )
    if after is not None:
        # A row comparison, so the (name, code) index can seek straight to the cursor.
        stmt = stmt.where(tuple_(Medicine.name, Medicine.code) > tuple_(*after))
    return stmt


def search_medicines(
    session: Session, q: str | None, after: tuple[str, str] | None, limit: int
) -> list[Medicine]:
    if q:
        # The planner only knows how selective an ILIKE pattern is once it sees the pattern.
        # psycopg prepares a statement after five uses and PostgreSQL may then switch to a
        # generic plan, which scans the (name, code) index and filters instead of using the
        # trigram indexes: two orders of magnitude slower on 300,000 medicines.
        session.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))
    return list(session.scalars(build_medicine_search_stmt(q, after, limit)))


def get_medicine_by_code(
    session: Session, code: str, *, lock: MedicineLock | None = None
) -> Medicine | None:
    stmt = select(Medicine).where(Medicine.code == code)
    if lock is not None:
        stmt = stmt.with_for_update(read=_LOCK_IS_SHARED[lock])
    return session.scalars(stmt).one_or_none()


def build_rules_stmt(medicine_id: int) -> Select[tuple[FormularyRule]]:
    return (
        select(FormularyRule)
        .where(FormularyRule.medicine_id == medicine_id)
        .order_by(FormularyRule.effective_from)
    )


def list_rules(session: Session, medicine_id: int) -> list[FormularyRule]:
    return list(session.scalars(build_rules_stmt(medicine_id)))


def end_rule(session: Session, rule: FormularyRule, effective_to: date) -> None:
    rule.effective_to = effective_to
    session.flush()


def add_rule(
    session: Session,
    *,
    medicine_id: int,
    effective_from: date,
    effective_to: date | None,
    max_quantity_per_dispense: int,
    max_quantity_per_30_days: int,
    requires_authorisation: bool,
) -> FormularyRule:
    rule = FormularyRule(
        medicine_id=medicine_id,
        effective_from=effective_from,
        effective_to=effective_to,
        max_quantity_per_dispense=max_quantity_per_dispense,
        max_quantity_per_30_days=max_quantity_per_30_days,
        requires_authorisation=requires_authorisation,
    )
    session.add(rule)
    session.flush()
    return rule


def dispense_summary(
    session: Session, medicine_id: int, *, start: datetime, end: datetime | None
) -> DispenseSummary | None:
    """The medicine's dispenses with `start <= dispensed_at < end` (no upper bound if `end` is
    None), or None if there are none."""
    stmt = select(
        func.count(), func.min(Dispense.dispensed_at), func.max(Dispense.dispensed_at)
    ).where(Dispense.medicine_id == medicine_id, Dispense.dispensed_at >= start)
    if end is not None:
        stmt = stmt.where(Dispense.dispensed_at < end)
    count, earliest, latest = session.execute(stmt).one()
    return DispenseSummary(count, earliest, latest) if count else None


def set_local_lock_timeout(session: Session, milliseconds: int) -> None:
    # set_config(..., true) is SET LOCAL with bind parameters: it lasts until the transaction ends.
    session.execute(select(func.set_config("lock_timeout", f"{milliseconds}ms", True)))


def ping(session: Session) -> None:
    session.execute(select(1))


def find_attempt_by_key(session: Session, idempotency_key: str) -> DispenseAttempt | None:
    stmt = select(DispenseAttempt).where(DispenseAttempt.idempotency_key == idempotency_key)
    return session.scalars(stmt).one_or_none()


def insert_attempt(
    session: Session,
    *,
    idempotency_key: str,
    request_hash: str,
    outcome: AttemptOutcome,
    medicine_code: str,
    medicine_id: int | None,
    patient_ref: str,
    quantity: int,
    dispensed_at: datetime,
    authorisation_ref: str | None,
    violations: list[dict[str, Any]],
    dispense_id: int | None,
) -> DispenseAttempt:
    attempt = DispenseAttempt(
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        outcome=outcome,
        medicine_code=medicine_code,
        medicine_id=medicine_id,
        patient_ref=patient_ref,
        quantity=quantity,
        dispensed_at=dispensed_at,
        authorisation_ref=authorisation_ref,
        violations=violations,
        dispense_id=dispense_id,
    )
    session.add(attempt)
    session.flush()
    return attempt


def insert_dispense(
    session: Session,
    *,
    medicine_id: int,
    rule_id: int,
    patient_ref: str,
    quantity: int,
    dispensed_at: datetime,
    authorisation_ref: str | None,
    idempotency_key: str,
) -> Dispense:
    dispense = Dispense(
        medicine_id=medicine_id,
        rule_id=rule_id,
        patient_ref=patient_ref,
        quantity=quantity,
        dispensed_at=dispensed_at,
        authorisation_ref=authorisation_ref,
        idempotency_key=idempotency_key,
    )
    session.add(dispense)
    session.flush()
    return dispense


def build_pair_history_stmt(
    medicine_id: int, patient_ref: str, start: datetime, end: datetime
) -> Select[tuple[int, datetime, int]]:
    return (
        select(Dispense.id, Dispense.dispensed_at, Dispense.quantity)
        .where(
            Dispense.patient_ref == patient_ref,
            Dispense.medicine_id == medicine_id,
            Dispense.dispensed_at >= start,
            Dispense.dispensed_at < end,
        )
        .order_by(Dispense.dispensed_at, Dispense.id)
    )


def pair_history(
    session: Session, *, medicine_id: int, patient_ref: str, start: datetime, end: datetime
) -> list[PriorDispense]:
    """What one patient has had of one medicine, dated `start <= dispensed_at < end`.

    Only meaningful while the patient-and-medicine lock is held: that is what makes the answer
    stay true until the caller has recorded its own dispense.
    """
    # Unlike search_medicines, this does not force a custom plan. After ten runs on one connection
    # PostgreSQL may settle on a generic plan, and for this query that plan can pick the
    # medicine's index and filter by patient: measured on 120,000 dispenses, 0.17 ms against 0.06
    # ms (plus 0.08 ms of planning) for the hottest medicine, and on 1.2 million it kept the
    # intended index. That is less than the round trip of forcing the plan on every dispense.
    rows = session.execute(build_pair_history_stmt(medicine_id, patient_ref, start, end))
    return [
        PriorDispense(id=dispense_id, dispensed_at=dispensed_at, quantity=quantity)
        for dispense_id, dispensed_at, quantity in rows
    ]


def build_dispense_list_stmt(
    patient_ref: str | None,
    medicine_id: int | None,
    after: tuple[datetime, int] | None,
    limit: int,
) -> Select[tuple[Dispense, str, str]]:
    # limit + 1: the extra row only tells the caller whether another page exists.
    stmt = (
        select(Dispense, Medicine.code, Medicine.name)
        .join(Medicine, Medicine.id == Dispense.medicine_id)
        .order_by(Dispense.dispensed_at.desc(), Dispense.id.desc())
        .limit(limit + 1)
    )
    if patient_ref is not None:
        stmt = stmt.where(Dispense.patient_ref == patient_ref)
    if medicine_id is not None:
        stmt = stmt.where(Dispense.medicine_id == medicine_id)
    if after is not None:
        # A row comparison, so the (dispensed_at DESC, id DESC) indexes can seek to the cursor.
        stmt = stmt.where(tuple_(Dispense.dispensed_at, Dispense.id) < tuple_(*after))
    return stmt


def list_dispenses(
    session: Session,
    patient_ref: str | None,
    medicine_id: int | None,
    after: tuple[datetime, int] | None,
    limit: int,
) -> list[DispenseRow]:
    return list(session.execute(build_dispense_list_stmt(patient_ref, medicine_id, after, limit)))


def get_dispense_with_medicine(session: Session, dispense_id: int) -> DispenseRow:
    stmt = (
        select(Dispense, Medicine.code, Medicine.name)
        .join(Medicine, Medicine.id == Dispense.medicine_id)
        .where(Dispense.id == dispense_id)
    )
    return session.execute(stmt).one()
