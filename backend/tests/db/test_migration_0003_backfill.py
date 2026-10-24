"""Migration 0003 gives dispenses that already exist a governing rule.

The legacy rows are created at revision 0002 with raw SQL, exactly as an installation that
predates the migration would hold them, and the migration is then run over them. The data is
chosen around the Africa/Johannesburg midnight, where the business date differs from the UTC
date, so that a backfill by UTC date would assign different rules (or none) and be caught.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import exc

from alembic import command
from tests.db.support import connect, current_revision, insert_medicine, insert_rule

ConfigFactory = Callable[[str], Config]


@dataclass(frozen=True)
class Seeded:
    """Rows seeded at revision 0002: label -> dispense id, and label -> governing rule id."""

    url: str
    dispenses: dict[str, int]
    governing_rule: dict[str, int]


def insert_legacy_dispense(db: psycopg.Connection, medicine_id: int, dispensed_at: str) -> int:
    """A dispense as revision 0002 stores it: there is no rule_id column yet."""
    row = db.execute(
        "INSERT INTO dispenses (medicine_id, patient_ref, quantity, dispensed_at, idempotency_key)"
        " VALUES (%s, 'PT-1', 1, %s, %s) RETURNING id",
        (medicine_id, dispensed_at, f"legacy-{uuid4().hex}"),
    ).fetchone()
    assert row is not None
    return row[0]


def column_names(url: str, table: str) -> list[str]:
    with connect(url, autocommit=True) as connection:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = %s ORDER BY column_name",
            (table,),
        )
        return [name for (name,) in rows]


def legacy_dispense_rows(url: str) -> list[tuple[object, ...]]:
    with connect(url, autocommit=True) as connection:
        return connection.execute(
            "SELECT id, medicine_id, patient_ref, quantity, dispensed_at, idempotency_key"
            " FROM dispenses ORDER BY id"
        ).fetchall()


def rules_by_label(seeded: Seeded) -> dict[str, int | None]:
    with connect(seeded.url, autocommit=True) as connection:
        by_id = dict(connection.execute("SELECT id, rule_id FROM dispenses").fetchall())
    return {label: by_id[dispense_id] for label, dispense_id in seeded.dispenses.items()}


def rule_chosen_by_utc_date(url: str, dispense_id: int) -> int | None:
    """The rule a backfill by UTC date would pick for this dispense, None if there is none."""
    with connect(url, autocommit=True) as connection:
        row = connection.execute(
            "SELECT r.id FROM dispenses d JOIN formulary_rules r ON r.medicine_id = d.medicine_id"
            " WHERE d.id = %s"
            " AND daterange(r.effective_from, r.effective_to, '[)')"
            " @> (d.dispensed_at AT TIME ZONE 'UTC')::date",
            (dispense_id,),
        ).fetchone()
    return None if row is None else row[0]


@pytest.fixture
def at_revision_0002(scratch_database: str, alembic_config: ConfigFactory) -> str:
    command.upgrade(alembic_config(scratch_database), "0002")
    return scratch_database


@pytest.fixture
def history(at_revision_0002: str) -> Seeded:
    """Two medicines with rule periods of different shapes, and dispenses on both sides of
    every boundary (SAST is UTC+2, so SAST midnight is 22:00:00 UTC the evening before)."""
    with connect(at_revision_0002, autocommit=True) as db:
        amoxil = insert_medicine(db, code="AMOXIL")
        brufen = insert_medicine(db, code="BRUFEN")
        first = insert_rule(db, amoxil, date(2025, 1, 1), date(2025, 6, 1))
        second = insert_rule(db, amoxil, date(2025, 6, 1), date(2025, 12, 1))
        open_ended = insert_rule(db, amoxil, date(2025, 12, 1), None)
        brufen_closed = insert_rule(db, brufen, date(2025, 3, 1), date(2025, 9, 1))
        brufen_open = insert_rule(db, brufen, date(2025, 9, 1), None)

        cases = {
            "first SAST instant of the first rule": (amoxil, "2024-12-31T22:00:00Z", first),
            "last second before SAST midnight": (amoxil, "2025-05-31T21:59:59Z", first),
            "SAST midnight, UTC date still the old day": (amoxil, "2025-05-31T22:00:00Z", second),
            "last second of a closed period": (amoxil, "2025-11-30T21:59:59Z", second),
            "first instant of the open-ended rule": (amoxil, "2025-11-30T22:00:00Z", open_ended),
            "far in the future, open-ended": (amoxil, "2031-07-15T08:00:00Z", open_ended),
            "other medicine, closed period": (brufen, "2025-07-10T09:00:00Z", brufen_closed),
            "other medicine, open-ended": (brufen, "2025-09-01T08:00:00Z", brufen_open),
        }
        dispenses = {
            label: insert_legacy_dispense(db, medicine, at)
            for label, (medicine, at, _) in cases.items()
        }
    return Seeded(
        at_revision_0002,
        dispenses,
        {label: rule for label, (_, _, rule) in cases.items()},
    )


def test_backfill_assigns_the_rule_in_force_on_the_sast_business_date(
    history: Seeded, alembic_config: ConfigFactory
) -> None:
    # Premise: for these two dispenses a backfill by UTC date would go wrong. The first has no
    # rule at all on its UTC date; the second would be given the rule that had already ended.
    first = history.dispenses["first SAST instant of the first rule"]
    midnight = history.dispenses["SAST midnight, UTC date still the old day"]
    assert rule_chosen_by_utc_date(history.url, first) is None
    assert (
        rule_chosen_by_utc_date(history.url, midnight)
        != (history.governing_rule["SAST midnight, UTC date still the old day"])
    )

    command.upgrade(alembic_config(history.url), "0003")

    assert rules_by_label(history) == history.governing_rule
    assert current_revision(history.url) == "0003"


def test_backfill_does_not_depend_on_the_session_time_zone(
    history: Seeded, alembic_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The migration's own connection sets no time zone, so libpq takes this one. Under UTC+14 a
    # plain timestamptz::date would put every boundary instant on the same calendar day.
    monkeypatch.setenv("PGOPTIONS", "-c timezone=Pacific/Kiritimati")

    command.upgrade(alembic_config(history.url), "0003")

    assert rules_by_label(history) == history.governing_rule


def test_downgrade_keeps_the_dispenses_and_upgrade_recomputes_the_same_links(
    history: Seeded, alembic_config: ConfigFactory
) -> None:
    config = alembic_config(history.url)
    before = legacy_dispense_rows(history.url)

    command.upgrade(config, "0003")
    command.downgrade(config, "0002")

    assert "rule_id" not in column_names(history.url, "dispenses")
    assert legacy_dispense_rows(history.url) == before
    assert current_revision(history.url) == "0002"

    command.upgrade(config, "0003")
    assert rules_by_label(history) == history.governing_rule


@dataclass(frozen=True)
class Gaps:
    """Rows seeded at revision 0002: dispenses that lack a governing rule (label -> id) and
    dispenses that have one (id -> that rule's id)."""

    url: str
    ungoverned: dict[str, int]
    governed: dict[int, int]
    amoxil: int
    lonely: int


@pytest.fixture
def gaps(at_revision_0002: str) -> Gaps:
    """Amoxil has rules for Jan-Feb and Apr-May 2025 only; Lonely has none. Governed dispenses
    are inserted first, so the offending ones get the higher ids."""
    with connect(at_revision_0002, autocommit=True) as db:
        amoxil = insert_medicine(db, code="AMOXIL")
        lonely = insert_medicine(db, code="LONELY")
        january = insert_rule(db, amoxil, date(2025, 1, 1), date(2025, 3, 1))
        april = insert_rule(db, amoxil, date(2025, 4, 1), date(2025, 6, 1))

        governed = {
            insert_legacy_dispense(db, amoxil, "2025-01-15T10:00:00Z"): january,
            insert_legacy_dispense(db, amoxil, "2025-04-10T10:00:00Z"): april,
        }
        ungoverned = {
            "before the first rule": (amoxil, "2024-12-31T21:59:59Z"),
            "first SAST day of the gap": (amoxil, "2025-02-28T22:00:00Z"),
            "inside the gap": (amoxil, "2025-03-15T10:00:00Z"),
            "last SAST second of the gap": (amoxil, "2025-03-31T21:59:59Z"),
            "the exclusive end of the last rule": (amoxil, "2025-05-31T22:00:00Z"),
            "after the last rule": (amoxil, "2026-01-01T00:00:00Z"),
            "medicine without any rule": (lonely, "2025-03-15T10:00:00Z"),
        }
        offending = {
            label: insert_legacy_dispense(db, medicine, at)
            for label, (medicine, at) in ungoverned.items()
        }
    return Gaps(at_revision_0002, offending, governed, amoxil, lonely)


def test_a_dispense_without_a_governing_rule_aborts_the_migration_and_names_the_offenders(
    gaps: Gaps, alembic_config: ConfigFactory
) -> None:
    before = legacy_dispense_rows(gaps.url)

    with pytest.raises(exc.DBAPIError) as failure:
        command.upgrade(alembic_config(gaps.url), "0003")

    error = failure.value.orig
    assert isinstance(error, psycopg.errors.RaiseException)
    assert error.diag.message_primary == (
        "7 dispense(s) have no formulary rule in force on their Africa/Johannesburg business date"
    )
    first_five = ", ".join(str(i) for i in sorted(gaps.ungoverned.values())[:5])
    assert error.diag.message_detail == f"First offending dispense ids: {first_five}"
    assert error.diag.message_hint is not None
    assert "Add the missing rule period(s)" in error.diag.message_hint

    # The failed migration left nothing behind: not even the nullable column it had added.
    assert current_revision(gaps.url) == "0002"
    assert "rule_id" not in column_names(gaps.url, "dispenses")
    assert legacy_dispense_rows(gaps.url) == before


def test_the_migration_succeeds_once_the_missing_rule_periods_exist(
    gaps: Gaps, alembic_config: ConfigFactory
) -> None:
    config = alembic_config(gaps.url)
    with pytest.raises(exc.DBAPIError):
        command.upgrade(config, "0003")

    with connect(gaps.url, autocommit=True) as db:
        before_first = insert_rule(db, gaps.amoxil, date(2024, 12, 1), date(2025, 1, 1))
        gap = insert_rule(db, gaps.amoxil, date(2025, 3, 1), date(2025, 4, 1))
        after_last = insert_rule(db, gaps.amoxil, date(2025, 6, 1), None)
        lonely = insert_rule(db, gaps.lonely, date(2025, 1, 1), None)

    command.upgrade(config, "0003")

    with connect(gaps.url, autocommit=True) as db:
        assigned = dict(db.execute("SELECT id, rule_id FROM dispenses").fetchall())
    # A period is [from, to): the dates at the old rule's exclusive end belong to the new one.
    expected = {
        "before the first rule": before_first,
        "first SAST day of the gap": gap,
        "inside the gap": gap,
        "last SAST second of the gap": gap,
        "the exclusive end of the last rule": after_last,
        "after the last rule": after_last,
        "medicine without any rule": lonely,
    }
    assert {label: assigned[i] for label, i in gaps.ungoverned.items()} == expected
    assert {i: assigned[i] for i in gaps.governed} == gaps.governed
    assert current_revision(gaps.url) == "0003"
