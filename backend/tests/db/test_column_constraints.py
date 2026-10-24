"""Column rules: NOT NULL, defaults, the CHECK constraints and the shape of a dispense attempt.

Each case starts from a valid row and breaks exactly one thing, then asserts that PostgreSQL
names the right constraint, so a constraint that silently stopped applying (or was renamed, and
would surface as an unmapped 500 in the API) fails here.
"""

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

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

Overrides = dict[str, Any]
Cases = list[tuple[str, Overrides]]


def describe(overrides: Overrides) -> str:
    """Readable test id; strings keep their quotes so padding and blanks are visible."""
    return ", ".join(
        f"{key}={value!r}" if isinstance(value, str) else f"{key}={value}"
        for key, value in overrides.items()
    )


def check_cases(cases: Cases) -> pytest.MarkDecorator:
    ids = [f"{constraint}[{describe(overrides)}]" for constraint, overrides in cases]
    return pytest.mark.parametrize(("constraint", "overrides"), cases, ids=ids)


def accepted_cases(cases: list[Overrides]) -> pytest.MarkDecorator:
    return pytest.mark.parametrize("overrides", cases, ids=[describe(case) for case in cases])


# -- NOT NULL and defaults --------------------------------------------------------------------

NOT_NULL = {
    "medicines": {
        "id",
        "code",
        "name",
        "form",
        "strength_value",
        "strength_unit",
        "is_active",
        "created_at",
    },
    "formulary_rules": {
        "id",
        "medicine_id",
        "effective_from",
        "max_quantity_per_dispense",
        "max_quantity_per_30_days",
        "requires_authorisation",
        "created_at",
    },
    "dispenses": {
        "id",
        "medicine_id",
        "patient_ref",
        "quantity",
        "dispensed_at",
        "idempotency_key",
        "created_at",
        "rule_id",
    },
    "dispense_attempts": {
        "id",
        "idempotency_key",
        "request_hash",
        "outcome",
        "medicine_code",
        "patient_ref",
        "quantity",
        "dispensed_at",
        "violations",
        "created_at",
    },
}


def test_exactly_the_specified_columns_are_not_null(db: psycopg.Connection) -> None:
    rows = db.execute(
        "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
        " WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'"
        " AND c.relname <> 'alembic_version' AND a.attnum > 0 AND NOT a.attisdropped"
        " AND a.attnotnull"
    ).fetchall()

    actual: dict[str, set[str]] = {table: set() for table in NOT_NULL}
    for table, column in rows:
        actual[table].add(column)
    assert actual == NOT_NULL


def test_server_defaults_fill_in_omitted_columns(
    db: psycopg.Connection, governed: tuple[int, int]
) -> None:
    medicine, rule = governed

    flags = db.execute(
        "INSERT INTO medicines (code, name, form, strength_value, strength_unit)"
        " VALUES (%s, 'N', 'F', 1, 'mg') RETURNING is_active, created_at = now()",
        (f"DEFAULTS-{uuid4().hex}",),
    ).fetchone()
    rule_defaults = db.execute(
        "INSERT INTO formulary_rules (medicine_id, effective_from, effective_to,"
        " max_quantity_per_dispense, max_quantity_per_30_days)"
        " VALUES (%s, '2031-01-01', '2032-01-01', 1, 1) RETURNING requires_authorisation",
        (insert_medicine(db),),
    ).fetchone()
    accepted_without_reasons = db.execute(
        "INSERT INTO dispense_attempts (idempotency_key, request_hash, outcome, medicine_code,"
        " patient_ref, quantity, dispensed_at, dispense_id)"
        " VALUES ('accepted-no-reasons', 'h', 'accepted', 'M', 'P', 1, now(), %s)"
        " RETURNING violations",
        (insert_dispense(db, medicine, rule),),
    ).fetchone()

    assert flags == (True, True)
    assert rule_defaults == (False,)
    assert accepted_without_reasons == ([],)


# -- CHECK constraints --------------------------------------------------------------------------

MEDICINE_VIOLATIONS: Cases = [
    ("ck_medicines_code_trimmed", {"code": ""}),
    ("ck_medicines_code_trimmed", {"code": " MED-1"}),
    ("ck_medicines_code_trimmed", {"code": "MED-1 "}),
    ("ck_medicines_name_not_blank", {"name": ""}),
    ("ck_medicines_name_not_blank", {"name": "   "}),
    ("ck_medicines_form_not_blank", {"form": "   "}),
    ("ck_medicines_strength_unit_not_blank", {"strength_unit": ""}),
    ("ck_medicines_strength_value_positive", {"strength_value": 0}),
    ("ck_medicines_strength_value_positive", {"strength_value": -1}),
    # numeric(12,3) rounds this to 0.000 before the CHECK sees it.
    ("ck_medicines_strength_value_positive", {"strength_value": Decimal("0.0004")}),
]


@check_cases(MEDICINE_VIOLATIONS)
def test_medicine_check_constraints_reject(
    db: psycopg.Connection, constraint: str, overrides: Overrides
) -> None:
    with violation(db, errors.CheckViolation, constraint):
        insert_medicine(db, **overrides)


@accepted_cases(
    [
        {"strength_value": Decimal("0.001")},
        {"code": "A B"},
        {"name": " padded "},
        {"is_active": False},
    ]
)
def test_medicine_values_next_to_the_checks_are_accepted(
    db: psycopg.Connection, overrides: Overrides
) -> None:
    insert_medicine(db, **overrides)


@accepted_cases([{"code": "\tMED-1"}, {"code": "MED-1\n"}, {"name": "\t"}])
def test_btrim_checks_trim_spaces_only(db: psycopg.Connection, overrides: Overrides) -> None:
    """The CHECKs use btrim, which strips spaces and nothing else, so a tab or a newline is not
    "blank" to the database. Control characters are rejected by the API's input validation
    (SPEC 3.8) before a value gets this far; the constraints are the safety net behind it."""
    insert_medicine(db, **overrides)


RULE_VIOLATIONS: Cases = [
    ("ck_formulary_rules_period_ordered", {"effective_to": date(2026, 3, 1)}),
    ("ck_formulary_rules_period_ordered", {"effective_to": date(2026, 2, 28)}),
    ("ck_formulary_rules_max_per_dispense_positive", {"max_quantity_per_dispense": 0}),
    ("ck_formulary_rules_max_per_dispense_positive", {"max_quantity_per_dispense": -1}),
    ("ck_formulary_rules_max_per_30_days_positive", {"max_quantity_per_30_days": 0}),
    ("ck_formulary_rules_max_per_30_days_positive", {"max_quantity_per_30_days": -1}),
]


@check_cases(RULE_VIOLATIONS)
def test_rule_check_constraints_reject(
    db: psycopg.Connection, constraint: str, overrides: Overrides
) -> None:
    medicine = insert_medicine(db)

    with violation(db, errors.CheckViolation, constraint):
        insert_rule(db, medicine, date(2026, 3, 1), **overrides)


@accepted_cases(
    [
        {"effective_to": date(2026, 3, 2)},
        {"effective_to": None},
        {"max_quantity_per_dispense": 1, "max_quantity_per_30_days": 1},
    ]
)
def test_rule_values_next_to_the_checks_are_accepted(
    db: psycopg.Connection, overrides: Overrides
) -> None:
    insert_rule(db, insert_medicine(db), date(2026, 3, 1), **overrides)


DISPENSE_VIOLATIONS: Cases = [
    ("ck_dispenses_quantity_positive", {"quantity": 0}),
    ("ck_dispenses_quantity_positive", {"quantity": -5}),
    ("ck_dispenses_patient_ref_not_blank", {"patient_ref": ""}),
    ("ck_dispenses_patient_ref_not_blank", {"patient_ref": "   "}),
    ("ck_dispenses_authorisation_ref_not_blank", {"authorisation_ref": ""}),
    ("ck_dispenses_authorisation_ref_not_blank", {"authorisation_ref": "   "}),
]


@check_cases(DISPENSE_VIOLATIONS)
def test_dispense_check_constraints_reject(
    db: psycopg.Connection, governed: tuple[int, int], constraint: str, overrides: Overrides
) -> None:
    with violation(db, errors.CheckViolation, constraint):
        insert_dispense(db, *governed, **overrides)


@accepted_cases([{"quantity": 1}, {"authorisation_ref": None}, {"authorisation_ref": "AUTH-1"}])
def test_dispense_values_next_to_the_checks_are_accepted(
    db: psycopg.Connection, governed: tuple[int, int], overrides: Overrides
) -> None:
    insert_dispense(db, *governed, **overrides)


@check_cases(
    [
        ("ck_dispense_attempts_quantity_positive", {"quantity": 0}),
        ("ck_dispense_attempts_quantity_positive", {"quantity": -1}),
    ]
)
def test_attempt_quantity_check_rejects(
    db: psycopg.Connection, constraint: str, overrides: Overrides
) -> None:
    with violation(db, errors.CheckViolation, constraint):
        insert_attempt(db, **overrides)


# -- The shape of an attempt: a rejection has reasons and no dispense, an acceptance the reverse


def test_a_rejection_with_reasons_and_no_dispense_is_accepted(db: psycopg.Connection) -> None:
    insert_attempt(db, outcome="rejected", dispense_id=None, violations=[{"code": "a"}])
    insert_attempt(db, violations=[{"code": "a"}, {"code": "b"}])


def test_an_acceptance_with_a_dispense_and_no_reasons_is_accepted(
    db: psycopg.Connection, governed: tuple[int, int]
) -> None:
    dispense = insert_dispense(db, *governed)

    insert_attempt(db, outcome="accepted", dispense_id=dispense, violations=[])


INVALID_SHAPES: dict[str, Callable[[int], Overrides]] = {
    "accepted without a dispense": lambda dispense: {
        "outcome": "accepted",
        "dispense_id": None,
        "violations": [],
    },
    "accepted but carrying violations": lambda dispense: {
        "outcome": "accepted",
        "dispense_id": dispense,
        "violations": [{"code": "x"}],
    },
    "rejected but pointing at a dispense": lambda dispense: {
        "outcome": "rejected",
        "dispense_id": dispense,
        "violations": [{"code": "x"}],
    },
    "rejected without any reason": lambda dispense: {
        "outcome": "rejected",
        "dispense_id": None,
        "violations": [],
    },
    "an outcome that is neither": lambda dispense: {
        "outcome": "pending",
        "dispense_id": None,
        "violations": [{"code": "x"}],
    },
}


@pytest.mark.parametrize("shape", INVALID_SHAPES.values(), ids=list(INVALID_SHAPES))
def test_an_attempt_of_the_wrong_shape_is_rejected(
    db: psycopg.Connection, governed: tuple[int, int], shape: Callable[[int], Overrides]
) -> None:
    dispense = insert_dispense(db, *governed)

    with violation(db, errors.CheckViolation, "ck_dispense_attempts_outcome_shape"):
        insert_attempt(db, **shape(dispense))


@pytest.mark.parametrize("violations", [{"code": "x"}, "x", 3], ids=["object", "string", "number"])
def test_violations_must_be_a_json_array(db: psycopg.Connection, violations: object) -> None:
    # Not the CHECK's doing: jsonb_array_length refuses a non-array before the CHECK can answer.
    with violation(db, errors.InvalidParameterValue, None):
        insert_attempt(db, violations=violations)
