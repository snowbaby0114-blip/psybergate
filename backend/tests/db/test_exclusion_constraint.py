"""ex_formulary_rules_no_overlap: no two rule periods of one medicine may overlap.

A period is the half-open range [effective_from, effective_to) and a NULL end means open-ended.
These tests talk to PostgreSQL directly, so the guarantee holds whatever the application does.
"""

from datetime import date

import psycopg
import pytest
from psycopg import errors

from tests.db.support import insert_medicine, insert_rule, violation

CONSTRAINT = "ex_formulary_rules_no_overlap"

Period = tuple[date, date | None]

CLOSED: Period = (date(2026, 3, 1), date(2026, 6, 1))
OPEN_ENDED: Period = (date(2026, 3, 1), None)

ACCEPTED_NEXT_TO_CLOSED: dict[str, Period] = {
    "starting where it ends": (date(2026, 6, 1), date(2026, 9, 1)),
    "ending where it starts": (date(2026, 1, 1), date(2026, 3, 1)),
    "open-ended, starting where it ends": (date(2026, 6, 1), None),
    "one day, starting where it ends": (date(2026, 6, 1), date(2026, 6, 2)),
    "one day, ending where it starts": (date(2026, 2, 28), date(2026, 3, 1)),
    "separated by a gap": (date(2026, 7, 1), date(2026, 8, 1)),
    "open-ended, far in the future": (date(2030, 1, 1), None),
}
REJECTED_AGAINST_CLOSED: dict[str, Period] = {
    "identical": (date(2026, 3, 1), date(2026, 6, 1)),
    "contained": (date(2026, 4, 1), date(2026, 5, 1)),
    "containing": (date(2026, 1, 1), date(2026, 12, 1)),
    "overlapping its start": (date(2026, 2, 1), date(2026, 3, 2)),
    "overlapping its end": (date(2026, 5, 31), date(2026, 7, 1)),
    "sharing only its first day": (date(2026, 3, 1), date(2026, 3, 2)),
    "sharing only its last day": (date(2026, 5, 31), date(2026, 6, 1)),
    "open-ended, covering it": (date(2026, 1, 1), None),
    "open-ended, starting inside it": (date(2026, 5, 1), None),
}
ACCEPTED_NEXT_TO_OPEN_ENDED: dict[str, Period] = {
    "ending exactly where it starts": (date(2026, 1, 1), date(2026, 3, 1)),
    "well before it": (date(2025, 1, 1), date(2025, 6, 1)),
}
REJECTED_AGAINST_OPEN_ENDED: dict[str, Period] = {
    "identical": (date(2026, 3, 1), None),
    "closed, starting the same day": (date(2026, 3, 1), date(2026, 4, 1)),
    "closed, far in the future": (date(2031, 1, 1), date(2031, 6, 1)),
    "open-ended, later": (date(2028, 1, 1), None),
    "closed, starting before and ending inside it": (date(2026, 1, 1), date(2026, 3, 2)),
}


@pytest.fixture
def medicine(db: psycopg.Connection) -> int:
    return insert_medicine(db)


def parametrize(periods: dict[str, Period]) -> pytest.MarkDecorator:
    return pytest.mark.parametrize("period", list(periods.values()), ids=list(periods))


@parametrize(ACCEPTED_NEXT_TO_CLOSED)
def test_period_next_to_a_closed_period_is_accepted(
    db: psycopg.Connection, medicine: int, period: Period
) -> None:
    insert_rule(db, medicine, *CLOSED)

    insert_rule(db, medicine, *period)


@parametrize(REJECTED_AGAINST_CLOSED)
def test_period_overlapping_a_closed_period_is_rejected(
    db: psycopg.Connection, medicine: int, period: Period
) -> None:
    insert_rule(db, medicine, *CLOSED)

    with violation(db, errors.ExclusionViolation, CONSTRAINT):
        insert_rule(db, medicine, *period)


@parametrize(ACCEPTED_NEXT_TO_OPEN_ENDED)
def test_period_next_to_an_open_ended_period_is_accepted(
    db: psycopg.Connection, medicine: int, period: Period
) -> None:
    insert_rule(db, medicine, *OPEN_ENDED)

    insert_rule(db, medicine, *period)


@parametrize(REJECTED_AGAINST_OPEN_ENDED)
def test_period_overlapping_an_open_ended_period_is_rejected(
    db: psycopg.Connection, medicine: int, period: Period
) -> None:
    insert_rule(db, medicine, *OPEN_ENDED)

    with violation(db, errors.ExclusionViolation, CONSTRAINT):
        insert_rule(db, medicine, *period)


def test_an_overlap_is_reported_as_sqlstate_23p01(db: psycopg.Connection, medicine: int) -> None:
    insert_rule(db, medicine, *CLOSED)

    with pytest.raises(psycopg.Error) as raised, db.transaction():
        insert_rule(db, medicine, *CLOSED)

    assert raised.value.sqlstate == "23P01"


def test_other_medicines_may_have_the_same_periods(db: psycopg.Connection, medicine: int) -> None:
    other = insert_medicine(db)
    insert_rule(db, medicine, *CLOSED)
    insert_rule(db, medicine, date(2026, 6, 1), None)

    insert_rule(db, other, *CLOSED)
    insert_rule(db, other, date(2026, 6, 1), None)


def test_extending_a_period_into_its_successor_is_rejected(
    db: psycopg.Connection, medicine: int
) -> None:
    first = insert_rule(db, medicine, date(2026, 3, 1), date(2026, 6, 1))
    insert_rule(db, medicine, date(2026, 6, 1), None)

    with violation(db, errors.ExclusionViolation, CONSTRAINT):
        db.execute(
            "UPDATE formulary_rules SET effective_to = %s WHERE id = %s",
            (date(2026, 6, 2), first),
        )


def test_superseding_works_by_shrinking_the_old_period_before_inserting_the_new_one(
    db: psycopg.Connection, medicine: int
) -> None:
    current = insert_rule(db, medicine, *OPEN_ENDED)
    new_start = date(2026, 9, 1)

    # Inserted first, the new period still collides with the old one, which is open-ended.
    with violation(db, errors.ExclusionViolation, CONSTRAINT):
        insert_rule(db, medicine, new_start, None)

    db.execute("UPDATE formulary_rules SET effective_to = %s WHERE id = %s", (new_start, current))
    insert_rule(db, medicine, new_start, None)

    timeline = db.execute(
        "SELECT effective_from, effective_to FROM formulary_rules"
        " WHERE medicine_id = %s ORDER BY effective_from",
        (medicine,),
    ).fetchall()
    assert timeline == [(date(2026, 3, 1), new_start), (new_start, None)]
