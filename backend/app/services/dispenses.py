"""Recording dispenses: validation against the rules in force on the dispense's own date, the
attempt log, idempotent replays, and the newest-first listing."""

from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import errors, locking, repositories
from app.domain import dispense as domain
from app.domain.idempotency import request_fingerprint
from app.domain.rules import RuleVersion
from app.domain.timeutil import history_bounds_utc, to_local_date
from app.models import Dispense, DispenseAttempt, Medicine
from app.pagination import InvalidCursor, decode_cursor, encode_cursor
from app.schemas import DispenseCreate, DispenseOut, Page
from app.services.formulary import to_rule_version

_IDEMPOTENCY_KEY_CONSTRAINTS = frozenset(
    {"uq_dispense_attempts_idempotency_key", "uq_dispenses_idempotency_key"}
)
_MAX_BIGINT = 2**63 - 1


# What a request came to. `replayed` marks an outcome read back from the attempt log instead of
# decided just now.
@dataclass(frozen=True)
class Created:
    dispense: DispenseOut
    replayed: bool = False


@dataclass(frozen=True)
class Rejected:
    attempt_id: int
    violations: tuple[domain.Violation, ...]
    replayed: bool = False


DispenseResult = Created | Rejected


def create_dispense(
    session: Session, request: DispenseCreate, *, now: datetime, lock_timeout_ms: int
) -> DispenseResult:
    """Decide the request and record the outcome, whichever it is, in one transaction.

    An accepted request leaves a dispense and an accepted attempt, a rejected one only a rejected
    attempt. Both commit, so a rejection is on record with its reasons and has created no
    dispense; the caller answers only afterwards. Any failure rolls everything back, so a client
    that retries with the same key after an error finds nothing half done.

    A key that was used before gets the original outcome again, or an error if the payload
    differs.
    """
    fingerprint = request_fingerprint(
        medicine_code=request.medicine_code,
        patient_ref=request.patient_ref,
        quantity=request.quantity,
        dispensed_at=request.dispensed_at,
        authorisation_ref=request.authorisation_ref,
    )
    try:
        return _record(session, request, fingerprint, now=now, lock_timeout_ms=lock_timeout_ms)
    except IntegrityError as conflict:
        if errors.constraint_name(conflict) not in _IDEMPOTENCY_KEY_CONSTRAINTS:
            raise
        return _replay_after_key_conflict(session, request, fingerprint, conflict)


def _record(
    session: Session,
    request: DispenseCreate,
    fingerprint: str,
    *,
    now: datetime,
    lock_timeout_ms: int,
) -> DispenseResult:
    """The transaction. It always takes its locks in this order, so that no two transactions can
    wait for each other: the idempotency key, the medicine row, the patient and medicine."""
    with session.begin():
        repositories.set_local_lock_timeout(session, lock_timeout_ms)
        # Requests with one key queue here, so that a duplicate finds the first one's outcome
        # below instead of racing it to the unique constraint.
        locking.lock_idempotency_key(session, request.idempotency_key)
        attempt = repositories.find_attempt_by_key(session, request.idempotency_key)
        if attempt is not None:
            return _replay(session, attempt, fingerprint)

        dispensed_at = request.dispensed_at if request.dispensed_at is not None else now
        # FOR SHARE: dispenses of one medicine run side by side, while a change of its rules
        # (FOR UPDATE) waits for them and is then checked against what they recorded.
        medicine = repositories.get_medicine_by_code(session, request.medicine_code, lock="share")
        if medicine is None:
            violations = (domain.medicine_not_found(request.medicine_code),)
            return _reject(session, request, fingerprint, None, dispensed_at, violations)

        evaluation = _evaluate(session, request, medicine, dispensed_at)
        if evaluation.rule is None or not evaluation.accepted:
            return _reject(
                session, request, fingerprint, medicine.id, dispensed_at, evaluation.violations
            )
        return _accept(session, request, fingerprint, medicine, evaluation.rule, dispensed_at)


def _evaluate(
    session: Session, request: DispenseCreate, medicine: Medicine, dispensed_at: datetime
) -> domain.Evaluation:
    locking.lock_patient_medicine(session, medicine.id, request.patient_ref)
    rules = repositories.list_rules(session, medicine.id)
    # Read only now that the lock is held: until this transaction ends nobody else can record a
    # dispense of this patient and medicine, so what is read here stays true until ours is in.
    start, end = history_bounds_utc(to_local_date(dispensed_at))
    history = repositories.pair_history(
        session, medicine_id=medicine.id, patient_ref=request.patient_ref, start=start, end=end
    )
    return domain.evaluate_dispense(
        medicine_active=medicine.is_active,
        rules=[to_rule_version(rule) for rule in rules],
        history=history,
        candidate=domain.Candidate(dispensed_at, request.quantity, request.authorisation_ref),
    )


def _accept(
    session: Session,
    request: DispenseCreate,
    fingerprint: str,
    medicine: Medicine,
    rule: RuleVersion,
    dispensed_at: datetime,
) -> Created:
    dispense = repositories.insert_dispense(
        session,
        medicine_id=medicine.id,
        rule_id=rule.id,
        patient_ref=request.patient_ref,
        quantity=request.quantity,
        dispensed_at=dispensed_at,
        authorisation_ref=request.authorisation_ref,
        idempotency_key=request.idempotency_key,
    )
    _log_attempt(session, request, fingerprint, medicine.id, dispensed_at, dispense_id=dispense.id)
    return Created(_dispense_out(dispense, medicine.code, medicine.name))


def _reject(
    session: Session,
    request: DispenseCreate,
    fingerprint: str,
    medicine_id: int | None,
    dispensed_at: datetime,
    violations: tuple[domain.Violation, ...],
) -> Rejected:
    attempt = _log_attempt(
        session, request, fingerprint, medicine_id, dispensed_at, violations=violations
    )
    return Rejected(attempt.id, _as_answered(violations))


def _log_attempt(
    session: Session,
    request: DispenseCreate,
    fingerprint: str,
    medicine_id: int | None,
    dispensed_at: datetime,
    *,
    violations: tuple[domain.Violation, ...] = (),
    dispense_id: int | None = None,
) -> DispenseAttempt:
    """The row every decided request leaves: accepted when it names the dispense it created,
    otherwise rejected with the violations exactly as the engine produced them. `dispensed_at` is
    the instant that was used, which is the server's now when the request gave none."""
    return repositories.insert_attempt(
        session,
        idempotency_key=request.idempotency_key,
        request_hash=fingerprint,
        outcome="accepted" if dispense_id is not None else "rejected",
        medicine_code=request.medicine_code,
        medicine_id=medicine_id,
        patient_ref=request.patient_ref,
        quantity=request.quantity,
        dispensed_at=dispensed_at,
        authorisation_ref=request.authorisation_ref,
        violations=[asdict(violation) for violation in violations],
        dispense_id=dispense_id,
    )


def _replay(session: Session, attempt: DispenseAttempt, fingerprint: str) -> DispenseResult:
    if attempt.request_hash != fingerprint:
        raise errors.idempotency_key_reuse()
    if attempt.dispense_id is not None:
        row = repositories.get_dispense_with_medicine(session, attempt.dispense_id)
        return Created(_dispense_out(*row), replayed=True)
    violations = _as_answered(domain.Violation(**stored) for stored in attempt.violations)
    return Rejected(attempt.id, violations, replayed=True)


def _as_answered(violations: Iterable[domain.Violation]) -> tuple[domain.Violation, ...]:
    """The violations as a rejection answers with them, the first time and on every replay.

    jsonb does not keep the order of the keys of an object, so what the attempt log returns is
    ordered differently from what the engine produced. Sorting the keys of every context on both
    sides makes a replayed body identical to the original to the byte, not only equal as JSON.
    """
    return tuple(
        replace(violation, context=_with_sorted_keys(violation.context)) for violation in violations
    )


def _with_sorted_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _with_sorted_keys(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_with_sorted_keys(item) for item in value]
    return value


def _replay_after_key_conflict(
    session: Session, request: DispenseCreate, fingerprint: str, conflict: IntegrityError
) -> DispenseResult:
    """Backstop for a request that was about to record its outcome when another one with the same
    key committed first. The key lock prevents that, so this only matters for a path that skips
    the lock. The failed transaction is already rolled back; the committed attempt is read in a
    new one and answered as any later request with the key would be."""
    with session.begin():
        attempt = repositories.find_attempt_by_key(session, request.idempotency_key)
        if attempt is None:
            raise conflict
        return _replay(session, attempt, fingerprint)


def list_dispenses(
    session: Session,
    *,
    patient_ref: str | None,
    medicine: str | None,
    limit: int,
    cursor: str | None,
) -> Page[DispenseOut]:
    # The cursor is bound to the filters: used with other ones it would resume from a position in
    # a different result set.
    filters = {"patient_ref": patient_ref, "medicine": medicine}
    after = _decode_position(cursor, filters)
    with session.begin():
        medicine_id = None
        if medicine is not None:
            found = repositories.get_medicine_by_code(session, medicine)
            if found is None:
                return Page[DispenseOut](items=[], next_cursor=None)
            medicine_id = found.id
        rows = repositories.list_dispenses(session, patient_ref, medicine_id, after, limit)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        position = [last.dispensed_at.astimezone(UTC).isoformat(timespec="microseconds"), last.id]
        next_cursor = encode_cursor(position, filters)
    return Page[DispenseOut](items=[_dispense_out(*row) for row in page], next_cursor=next_cursor)


def _dispense_out(dispense: Dispense, medicine_code: str, medicine_name: str) -> DispenseOut:
    return DispenseOut(
        id=dispense.id,
        medicine_code=medicine_code,
        medicine_name=medicine_name,
        patient_ref=dispense.patient_ref,
        quantity=dispense.quantity,
        dispensed_at=dispense.dispensed_at,
        authorisation_ref=dispense.authorisation_ref,
        idempotency_key=dispense.idempotency_key,
        rule_id=dispense.rule_id,
        created_at=dispense.created_at,
    )


def _decode_position(cursor: str | None, filters: dict[str, Any]) -> tuple[datetime, int] | None:
    if cursor is None:
        return None
    try:
        key = decode_cursor(cursor, filters)
    except InvalidCursor as exc:
        raise errors.invalid_cursor() from exc
    position = _parse_position(key)
    if position is None:
        raise errors.invalid_cursor()
    return position


def _parse_position(key: list[Any]) -> tuple[datetime, int] | None:
    """The (instant, id) a cursor holds, if it is what next_cursor would have put there.

    Cursors are not signed, so their content is client input. The instant must be spelt exactly as
    next_cursor spells it, in UTC with microseconds: fromisoformat accepts far more than that
    (even a trailing NUL), and a UTC instant needs no conversion, which could overflow.
    """
    if len(key) != 2:
        return None
    instant, dispense_id = key
    if not isinstance(instant, str) or type(dispense_id) is not int:
        return None
    if not 0 <= dispense_id <= _MAX_BIGINT:
        return None
    try:
        moment = datetime.fromisoformat(instant)
    except ValueError:
        return None
    if moment.utcoffset() != timedelta(0) or moment.isoformat(timespec="microseconds") != instant:
        return None
    return moment, dispense_id
