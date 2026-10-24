"""The self-check has to be able to fail: each corruption below is applied inside a transaction
that is rolled back, and the check that guards the broken invariant must say so."""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Connection, Engine, text

from app.domain.timeutil import local_day_start_utc
from app.seed import (
    DEMO_AUTH,
    DEMO_HISTORY,
    DEMO_INACTIVE,
    DEMO_PATIENT,
    DEMO_TIGHT,
    SeedSummary,
    verify,
)
from tests.seed.support import TODAY, YEARS

INACTIVE = f"(SELECT id FROM medicines WHERE code = '{DEMO_INACTIVE}')"
HISTORY = f"(SELECT id FROM medicines WHERE code = '{DEMO_HISTORY}')"
AUTH = f"(SELECT id FROM medicines WHERE code = '{DEMO_AUTH}')"
TIGHT = f"(SELECT id FROM medicines WHERE code = '{DEMO_TIGHT}')"
FIRST_RULE_OF_INACTIVE = (
    f"medicine_id = {INACTIVE} AND effective_from = "
    f"(SELECT min(effective_from) FROM formulary_rules WHERE medicine_id = {INACTIVE})"
)
SHIFT_ITS_END = "UPDATE formulary_rules SET effective_to = effective_to"
MOVE_ITS_START = "UPDATE formulary_rules SET effective_from = effective_to - 1"

PARAMETERS = {
    "today": TODAY,
    "opens_cutoff": local_day_start_utc(TODAY - timedelta(days=YEARS * 365 - 10)),
    "ends_cutoff": local_day_start_utc(TODAY - timedelta(days=10)),
}

CORRUPTIONS = [
    pytest.param(
        [f"DELETE FROM formulary_rules WHERE medicine_id = {INACTIVE} AND effective_to IS NULL"],
        "3 to 5 rule periods",
        id="a medicine left with two rule periods",
    ),
    pytest.param(
        [f"{SHIFT_ITS_END} - 1 WHERE {FIRST_RULE_OF_INACTIVE}"],
        "tile each medicine's timeline",
        id="a day with no rule between two periods",
    ),
    pytest.param(
        [
            "ALTER TABLE formulary_rules DROP CONSTRAINT ex_formulary_rules_no_overlap",
            f"{SHIFT_ITS_END} + 1 WHERE {FIRST_RULE_OF_INACTIVE}",
        ],
        "no two periods of a medicine overlap",
        id="two periods overlapping (once the constraint is out of the way)",
    ),
    pytest.param(
        [f"{MOVE_ITS_START} WHERE {FIRST_RULE_OF_INACTIVE}"],
        "starts early enough",
        id="rule history that starts too late",
    ),
    pytest.param(
        [
            """
            WITH target AS (
                SELECT d.id AS dispense_id, (
                    SELECT r.id FROM formulary_rules r
                    WHERE r.medicine_id = d.medicine_id AND r.id <> d.rule_id ORDER BY r.id LIMIT 1
                ) AS other_rule
                FROM dispenses d WHERE d.patient_ref <> 'PT-DEMO' ORDER BY d.id LIMIT 1
            )
            UPDATE dispenses SET rule_id = target.other_rule
            FROM target WHERE dispenses.id = target.dispense_id
            """
        ],
        "covers its SAST business date",
        id="a dispense pointing at a rule from another period",
    ),
    pytest.param(
        [
            f"""
            UPDATE dispenses SET rule_id = (
                SELECT r.id FROM formulary_rules r
                WHERE r.medicine_id = dispenses.medicine_id
                  AND r.effective_to = (SELECT effective_from FROM formulary_rules
                                        WHERE id = dispenses.rule_id)
            )
            WHERE id = (SELECT id FROM dispenses
                        WHERE patient_ref = '{DEMO_PATIENT}' AND medicine_id = {HISTORY}
                        ORDER BY dispensed_at DESC LIMIT 1)
            """
        ],
        "covers its SAST business date",
        id="the rule of the UTC date instead of the SAST date at midnight",
    ),
    pytest.param(
        [
            (
                f"UPDATE dispenses SET quantity = 61 WHERE patient_ref = '{DEMO_PATIENT}'"
                f" AND medicine_id = {AUTH} AND quantity = 30"
            )
        ],
        "per-dispense limit",
        id="a quantity above the per-dispense limit",
    ),
    pytest.param(
        [
            f"""
            UPDATE dispenses SET quantity = 10 WHERE id = (
                SELECT id FROM dispenses WHERE patient_ref = '{DEMO_PATIENT}'
                  AND medicine_id = {TIGHT} ORDER BY dispensed_at DESC LIMIT 1)
            """
        ],
        "30-day limit",
        id="a 30-business-day window above the cap",
    ),
    pytest.param(
        [
            (
                "UPDATE dispenses SET authorisation_ref = NULL"
                f" WHERE patient_ref = '{DEMO_PATIENT}' AND medicine_id = {AUTH}"
            )
        ],
        "authorisation reference its rule requires",
        id="a missing authorisation reference",
    ),
    pytest.param(
        [f"UPDATE medicines SET is_active = false WHERE code = '{DEMO_TIGHT}'"],
        "inactive medicine",
        id="a dispense of a medicine that is inactive",
    ),
    pytest.param(
        ["DELETE FROM dispense_attempts WHERE id = (SELECT min(id) FROM dispense_attempts)"],
        "matching accepted attempt",
        id="a dispense whose attempt is gone",
    ),
    pytest.param(
        [
            (
                "INSERT INTO dispense_attempts (idempotency_key, request_hash, outcome,"
                " medicine_code, patient_ref, quantity, dispensed_at, violations)"
                " VALUES ('extra', 'h', 'rejected', 'X', 'PT-1', 1, now(),"
                ' \'[{"code": "x"}]\'::jsonb)'
            )
        ],
        "no others",
        id="an attempt that belongs to no dispense",
    ),
    pytest.param(
        [
            (
                "UPDATE dispenses SET created_at = now() + interval '1 day'"
                " WHERE id = (SELECT min(id) FROM dispenses)"
            )
        ],
        "logged before it happened or in the future",
        id="a dispense logged in the future",
    ),
    pytest.param(
        [
            (
                "UPDATE dispenses SET created_at = dispensed_at - interval '1 minute'"
                " WHERE id = (SELECT min(id) FROM dispenses)"
            )
        ],
        "logged before it happened or in the future",
        id="a dispense logged before it happened",
    ),
    pytest.param(
        [
            f"DELETE FROM formulary_rules WHERE medicine_id = {INACTIVE}",
            f"DELETE FROM medicines WHERE code = '{DEMO_INACTIVE}'",
        ],
        "medicines stored",
        id="a medicine missing",
    ),
    pytest.param(
        [
            "DELETE FROM dispense_attempts WHERE dispense_id = (SELECT min(id) FROM dispenses)",
            "DELETE FROM dispenses WHERE id = (SELECT min(id) FROM dispenses)",
        ],
        "dispenses stored",
        id="a dispense missing",
    ),
    pytest.param(
        ["UPDATE dispenses SET patient_ref = 'PT-DEMO'"],
        "no fewer patient refs",
        id="patients merged into one",
    ),
    pytest.param(
        [
            (
                "DELETE FROM dispense_attempts WHERE dispense_id IN"
                " (SELECT id FROM dispenses WHERE dispensed_at < :opens_cutoff)"
            ),
            "DELETE FROM dispenses WHERE dispensed_at < :opens_cutoff",
        ],
        "history opens within a week",
        id="the oldest dispenses missing",
    ),
    pytest.param(
        [
            (
                "DELETE FROM dispense_attempts WHERE dispense_id IN"
                " (SELECT id FROM dispenses WHERE dispensed_at >= :ends_cutoff)"
            ),
            "DELETE FROM dispenses WHERE dispensed_at >= :ends_cutoff",
        ],
        "history ends within the last week",
        id="the newest dispenses missing",
    ),
    pytest.param(
        ["DELETE FROM formulary_rules WHERE effective_from > :today"],
        "scheduled to start after today",
        id="no rule scheduled for the future",
    ),
]


@pytest.fixture
def connection(engine: Engine, seeded: SeedSummary) -> Iterator[Connection]:
    """A connection whose changes are always rolled back, so each test sees the seeded data."""
    with engine.connect() as connection:
        yield connection
        connection.rollback()


def failed_checks(connection: Connection, summary: SeedSummary) -> list[str]:
    return [check.name for check in verify(connection, summary.expected).checks if not check.ok]


def test_a_fresh_seed_passes_every_check(connection: Connection, seeded: SeedSummary) -> None:
    verification = verify(connection, seeded.expected)

    assert seeded.ok
    assert verification.ok
    assert len(verification.checks) >= 15
    assert verification.stats.dispenses == seeded.expected.dispenses


@pytest.mark.parametrize(("statements", "failing_check"), CORRUPTIONS)
def test_each_invariant_is_reported_when_it_is_broken(
    connection: Connection, seeded: SeedSummary, statements: list[str], failing_check: str
) -> None:
    for statement in statements:
        connection.execute(text(statement), PARAMETERS)

    failed = failed_checks(connection, seeded)

    assert any(failing_check in name for name in failed), failed


def test_a_failing_check_says_how_many_rows_break_it(
    connection: Connection, seeded: SeedSummary
) -> None:
    connection.execute(
        text(
            f"UPDATE dispenses SET quantity = 10 WHERE id = (SELECT id FROM dispenses"
            f" WHERE patient_ref = '{DEMO_PATIENT}' AND medicine_id = {TIGHT}"
            " ORDER BY dispensed_at DESC LIMIT 1)"
        )
    )

    window = next(c for c in verify(connection, seeded.expected).checks if "30-day" in c.name)

    assert not window.ok
    assert window.detail == "offending rows: 1"


def test_an_empty_database_fails_the_check_instead_of_crashing_it(
    engine: Engine, seeded: SeedSummary
) -> None:
    with engine.connect() as connection:
        connection.execute(
            text(
                "TRUNCATE medicines, formulary_rules, dispenses, dispense_attempts"
                " RESTART IDENTITY CASCADE"
            )
        )

        verification = verify(connection, seeded.expected)

        connection.rollback()
    assert not verification.ok
    assert verification.stats.oldest is None
    assert {c.name for c in verification.checks if not c.ok} >= {
        "medicines stored",
        "dispenses stored",
        "history opens within a week of the window start",
    }


@pytest.mark.parametrize(("days_apart", "breaks_the_cap"), [(30, False), (29, True)])
def test_the_window_reaches_back_exactly_29_days_from_the_dispense(
    connection: Connection, seeded: SeedSummary, days_apart: int, breaks_the_cap: bool
) -> None:
    # PT-DEMO's three DEMO-TIGHT dispenses become 10 units each against a cap of 20 per window.
    # The middle one is 15 days before the last; the first is `days_apart` before the last, so
    # it is inside the last one's window at 29 days and outside it at 30.
    connection.execute(
        text(
            f"""
            WITH tight AS (
                SELECT id, row_number() OVER (ORDER BY dispensed_at) AS position
                FROM dispenses
                WHERE patient_ref = '{DEMO_PATIENT}' AND medicine_id = {TIGHT}
            ), last AS (
                SELECT dispensed_at FROM dispenses
                WHERE id = (SELECT id FROM tight WHERE position = 3)
            )
            UPDATE dispenses SET quantity = 10, dispensed_at = CASE tight.position
                WHEN 1 THEN last.dispensed_at - make_interval(days => {days_apart})
                WHEN 2 THEN last.dispensed_at - make_interval(days => 15)
                ELSE last.dispensed_at END
            FROM tight, last WHERE dispenses.id = tight.id
            """
        )
    )

    window = next(c for c in verify(connection, seeded.expected).checks if "30-day" in c.name)

    assert window.ok is not breaks_the_cap
