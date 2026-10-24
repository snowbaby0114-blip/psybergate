"""Unique constraints, foreign keys (including the composite rule/medicine one) and identity ids.

The foreign keys are the schema's memory: a dispense can never point at a rule that belongs to
another medicine, and nothing the history refers to can be deleted out from under it.
"""

from collections.abc import Callable
from datetime import date

import psycopg
import pytest
from psycopg import errors

from tests.db.support import (
    insert_attempt,
    insert_dispense,
    insert_medicine,
    insert_rule,
    violation,
)

MISSING = 999_999_999
Governed = tuple[int, int]


# -- Unique constraints -----------------------------------------------------------------------


def test_medicine_codes_are_unique(db: psycopg.Connection) -> None:
    insert_medicine(db, code="DUP-1")

    with violation(db, errors.UniqueViolation, "uq_medicines_code"):
        insert_medicine(db, code="DUP-1")


def test_dispense_idempotency_keys_are_unique(db: psycopg.Connection, governed: Governed) -> None:
    insert_dispense(db, *governed, idempotency_key="key-1")

    with violation(db, errors.UniqueViolation, "uq_dispenses_idempotency_key"):
        insert_dispense(db, *governed, idempotency_key="key-1")


def test_attempt_idempotency_keys_are_unique(db: psycopg.Connection) -> None:
    insert_attempt(db, idempotency_key="key-1")

    with violation(db, errors.UniqueViolation, "uq_dispense_attempts_idempotency_key"):
        insert_attempt(db, idempotency_key="key-1")


def test_a_dispense_and_its_attempt_share_the_idempotency_key(
    db: psycopg.Connection, governed: Governed
) -> None:
    dispense = insert_dispense(db, *governed, idempotency_key="key-1")

    insert_attempt(
        db, idempotency_key="key-1", outcome="accepted", dispense_id=dispense, violations=[]
    )


def test_a_dispense_has_at_most_one_attempt(db: psycopg.Connection, governed: Governed) -> None:
    dispense = insert_dispense(db, *governed)
    insert_attempt(db, outcome="accepted", dispense_id=dispense, violations=[])

    with violation(db, errors.UniqueViolation, "uq_dispense_attempts_dispense_id"):
        insert_attempt(db, outcome="accepted", dispense_id=dispense, violations=[])


def test_any_number_of_rejected_attempts_may_lack_a_dispense(db: psycopg.Connection) -> None:
    for _ in range(3):
        insert_attempt(db, outcome="rejected", dispense_id=None)


# -- Foreign keys: a child needs its parent ---------------------------------------------------


def test_a_rule_needs_an_existing_medicine(db: psycopg.Connection) -> None:
    with violation(db, errors.ForeignKeyViolation, "fk_formulary_rules_medicine_id"):
        insert_rule(db, MISSING, date(2026, 1, 1))


def test_a_dispense_needs_an_existing_rule(db: psycopg.Connection, governed: Governed) -> None:
    medicine, _ = governed

    with violation(db, errors.ForeignKeyViolation, "fk_dispenses_rule_id_medicine_id"):
        insert_dispense(db, medicine, MISSING)


def test_a_dispense_cannot_be_governed_by_another_medicines_rule(
    db: psycopg.Connection, governed: Governed
) -> None:
    medicine, rule = governed
    other_medicine = insert_medicine(db)
    other_rule = insert_rule(db, other_medicine, date(2025, 1, 1))

    with violation(db, errors.ForeignKeyViolation, "fk_dispenses_rule_id_medicine_id"):
        insert_dispense(db, medicine, other_rule)
    with violation(db, errors.ForeignKeyViolation, "fk_dispenses_rule_id_medicine_id"):
        insert_dispense(db, other_medicine, rule)

    insert_dispense(db, medicine, rule)
    insert_dispense(db, other_medicine, other_rule)


def test_a_dispense_cannot_be_moved_onto_another_medicines_rule(
    db: psycopg.Connection, governed: Governed
) -> None:
    dispense = insert_dispense(db, *governed)
    other_rule = insert_rule(db, insert_medicine(db), date(2025, 1, 1))

    with violation(db, errors.ForeignKeyViolation, "fk_dispenses_rule_id_medicine_id"):
        db.execute("UPDATE dispenses SET rule_id = %s WHERE id = %s", (other_rule, dispense))


def test_a_dispense_needs_an_existing_medicine(db: psycopg.Connection, governed: Governed) -> None:
    _, rule = governed

    with pytest.raises(errors.ForeignKeyViolation) as raised, db.transaction():
        insert_dispense(db, MISSING, rule)

    # The plain medicine key is implied by the composite one, and either may report first.
    assert raised.value.diag.constraint_name in {
        "fk_dispenses_medicine_id",
        "fk_dispenses_rule_id_medicine_id",
    }


def test_an_attempt_needs_an_existing_medicine_when_it_names_one(db: psycopg.Connection) -> None:
    with violation(db, errors.ForeignKeyViolation, "fk_dispense_attempts_medicine_id"):
        insert_attempt(db, medicine_id=MISSING)


def test_an_accepted_attempt_needs_an_existing_dispense(db: psycopg.Connection) -> None:
    with violation(db, errors.ForeignKeyViolation, "fk_dispense_attempts_dispense_id"):
        insert_attempt(db, outcome="accepted", dispense_id=MISSING, violations=[])


# -- Foreign keys: the history cannot be pulled out from under its references -------------------


def test_a_medicine_with_rules_cannot_be_deleted(db: psycopg.Connection) -> None:
    medicine = insert_medicine(db)
    insert_rule(db, medicine, date(2026, 1, 1))

    with violation(db, errors.ForeignKeyViolation, "fk_formulary_rules_medicine_id"):
        db.execute("DELETE FROM medicines WHERE id = %s", (medicine,))


def test_a_medicine_named_by_an_attempt_cannot_be_deleted(db: psycopg.Connection) -> None:
    medicine = insert_medicine(db)
    insert_attempt(db, medicine_id=medicine)

    with violation(db, errors.ForeignKeyViolation, "fk_dispense_attempts_medicine_id"):
        db.execute("DELETE FROM medicines WHERE id = %s", (medicine,))


def test_a_rule_that_governed_a_dispense_cannot_be_deleted(
    db: psycopg.Connection, governed: Governed
) -> None:
    _, rule = governed
    insert_dispense(db, *governed)

    with violation(db, errors.ForeignKeyViolation, "fk_dispenses_rule_id_medicine_id"):
        db.execute("DELETE FROM formulary_rules WHERE id = %s", (rule,))


def test_a_dispense_with_an_attempt_cannot_be_deleted(
    db: psycopg.Connection, governed: Governed
) -> None:
    dispense = insert_dispense(db, *governed)
    insert_attempt(db, outcome="accepted", dispense_id=dispense, violations=[])

    with violation(db, errors.ForeignKeyViolation, "fk_dispense_attempts_dispense_id"):
        db.execute("DELETE FROM dispenses WHERE id = %s", (dispense,))


def test_a_rule_that_governed_a_dispense_cannot_be_moved_to_another_medicine(
    db: psycopg.Connection, governed: Governed
) -> None:
    medicine, rule = governed
    insert_dispense(db, *governed)
    other_medicine = insert_medicine(db)
    unused_rule = insert_rule(db, medicine, date(2020, 1, 1), date(2021, 1, 1))

    with violation(db, errors.ForeignKeyViolation, "fk_dispenses_rule_id_medicine_id"):
        db.execute(
            "UPDATE formulary_rules SET medicine_id = %s WHERE id = %s", (other_medicine, rule)
        )

    # Only referenced rules are protected: one that governed nothing can still be re-parented.
    db.execute(
        "UPDATE formulary_rules SET medicine_id = %s WHERE id = %s", (other_medicine, unused_rule)
    )


# -- Identity columns -------------------------------------------------------------------------

EXPLICIT_ID_INSERTS: dict[str, Callable[[psycopg.Connection, Governed], int]] = {
    "medicines": lambda db, governed: insert_medicine(db, id=1),
    "formulary_rules": lambda db, governed: insert_rule(
        db, insert_medicine(db), date(2031, 1, 1), id=1
    ),
    "dispenses": lambda db, governed: insert_dispense(db, *governed, id=1),
    "dispense_attempts": lambda db, governed: insert_attempt(db, id=1),
}


@pytest.mark.parametrize("insert", EXPLICIT_ID_INSERTS.values(), ids=list(EXPLICIT_ID_INSERTS))
def test_ids_are_always_generated_and_cannot_be_chosen(
    db: psycopg.Connection,
    governed: Governed,
    insert: Callable[[psycopg.Connection, Governed], int],
) -> None:
    with violation(db, errors.GeneratedAlways, None):
        insert(db, governed)


def test_an_existing_id_cannot_be_rewritten(db: psycopg.Connection) -> None:
    medicine = insert_medicine(db)

    with violation(db, errors.GeneratedAlways, None):
        db.execute("UPDATE medicines SET id = id + 1000 WHERE id = %s", (medicine,))


def test_generated_ids_increase(db: psycopg.Connection) -> None:
    ids = [insert_medicine(db) for _ in range(3)]

    assert ids == sorted(set(ids))
