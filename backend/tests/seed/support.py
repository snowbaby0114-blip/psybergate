"""Shared helpers for the seed tests."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from random import Random
from typing import Any

from sqlalchemy import Connection, Engine, text

from app.domain.dispense import Candidate, Evaluation, PriorDispense, evaluate_dispense
from app.domain.rules import RuleVersion, rule_in_force
from app.domain.timeutil import to_local_date, window_start_date
from app.seed import (
    TABLES,
    AdmittedDispense,
    CatalogueEntry,
    DispenseWindow,
    Ledger,
    build_entries,
    generate_catalogue,
    generate_dispenses,
    generate_patient_refs,
)

# A fixed date in the past: nothing here may depend on the wall clock.
TODAY = date(2026, 6, 15)
YEARS = 2

# The small dataset the database tests seed.
RANDOM_SEED = 20260901
MEDICINES = 60
DISPENSES = 300
PATIENTS = 150


@dataclass(frozen=True)
class PureDataset:
    """What the generators produce before anything reaches a database, with made-up ids."""

    entries: list[CatalogueEntry]
    patients: list[str]
    window: DispenseWindow
    ledger: Ledger


def build_pure_dataset(
    *, medicines: int, dispenses: int, patients: int, seed: str = "pure", years: int = YEARS
) -> PureDataset:
    specs = generate_catalogue(Random(f"{seed}/medicines"), medicines, TODAY, years)
    medicine_ids = {spec.code: number for number, spec in enumerate(specs, start=1)}
    rule_ids = {
        (medicine_ids[spec.code], period.effective_from): medicine_ids[spec.code] * 10 + index
        for spec in specs
        for index, period in enumerate(spec.periods)
    }
    entries = build_entries(specs, medicine_ids, rule_ids)
    refs = generate_patient_refs(Random(f"{seed}/patients"), patients)
    window = DispenseWindow(TODAY, years * 365)
    ledger = generate_dispenses(Random(f"{seed}/dispenses"), entries, refs, dispenses, window)
    return PureDataset(entries, refs, window, ledger)


def rule_of(entries: list[CatalogueEntry], dispense: AdmittedDispense) -> RuleVersion:
    medicine = next(entry for entry in entries if entry.id == dispense.medicine.id)
    return next(rule for rule in medicine.rules if rule.id == dispense.rule_id)


def window_total(pair: list[AdmittedDispense], day: date) -> int:
    """The quantity dispensed on the 30 business days ending on `day`, by brute force."""
    first = window_start_date(day)
    return sum(d.quantity for d in pair if first <= to_local_date(d.dispensed_at) <= day)


def broken_rules(entries: list[CatalogueEntry], admitted: list[AdmittedDispense]) -> list[str]:
    """Every way in which the admitted dispenses break a rule, worked out independently of the
    engine: from the rules and the business dates alone."""
    pairs: dict[tuple[str, int], list[AdmittedDispense]] = {}
    for dispense in admitted:
        pairs.setdefault((dispense.patient_ref, dispense.medicine.id), []).append(dispense)

    problems = []
    for dispense in admitted:
        day = to_local_date(dispense.dispensed_at)
        medicine = next(entry for entry in entries if entry.id == dispense.medicine.id)
        in_force = rule_in_force(medicine.rules, day)
        rule = rule_of(entries, dispense)
        label = f"{dispense.patient_ref}/{medicine.code}/{day}"
        if not medicine.is_active:
            problems.append(f"{label}: inactive medicine")
        if in_force is None or in_force.id != dispense.rule_id:
            problems.append(f"{label}: rule {dispense.rule_id} is not the rule in force")
        if dispense.quantity > rule.max_quantity_per_dispense:
            problems.append(f"{label}: above the per-dispense limit")
        if rule.requires_authorisation and not dispense.authorisation_ref:
            problems.append(f"{label}: authorisation missing")
        pair = pairs[(dispense.patient_ref, dispense.medicine.id)]
        if window_total(pair, day) > rule.max_quantity_per_30_days:
            problems.append(f"{label}: above the 30-day limit")
    return problems


def pairs_with_dispenses_inside_one_window(admitted: list[AdmittedDispense]) -> int:
    """How many (patient, medicine) pairs have two dispenses within 30 business days."""
    days: dict[tuple[str, int], list[date]] = {}
    for dispense in admitted:
        key = (dispense.patient_ref, dispense.medicine.id)
        days.setdefault(key, []).append(to_local_date(dispense.dispensed_at))
    close = 0
    for dates in days.values():
        dates.sort()
        if any(later - earlier < timedelta(days=30) for earlier, later in pairwise(dates)):
            close += 1
    return close


def rows(connection: Connection, sql: str, **parameters: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(text(sql), parameters).mappings()]


def dump(engine: Engine) -> dict[str, list[dict[str, Any]]]:
    """Every row of every seeded table, in id order, for comparing two states of the database."""
    with engine.connect() as connection:
        return {table: rows(connection, f"SELECT * FROM {table} ORDER BY id") for table in TABLES}


def stored_rules(connection: Connection) -> dict[int, list[RuleVersion]]:
    """The stored rules of every medicine, oldest first, as the engine sees them."""
    rules: dict[int, list[RuleVersion]] = {}
    for row in rows(connection, "SELECT * FROM formulary_rules ORDER BY effective_from"):
        rules.setdefault(row["medicine_id"], []).append(
            RuleVersion(
                id=row["id"],
                effective_from=row["effective_from"],
                effective_to=row["effective_to"],
                max_quantity_per_dispense=row["max_quantity_per_dispense"],
                max_quantity_per_30_days=row["max_quantity_per_30_days"],
                requires_authorisation=row["requires_authorisation"],
            )
        )
    return rules


def evaluate_stored(
    connection: Connection,
    *,
    patient_ref: str,
    code: str,
    quantity: int,
    at: datetime,
    authorisation_ref: str | None = None,
) -> Evaluation:
    """What the engine says about one more dispense, given exactly what is stored."""
    medicine = rows(connection, "SELECT id, is_active FROM medicines WHERE code = :code", code=code)
    history = rows(
        connection,
        "SELECT id, dispensed_at, quantity FROM dispenses"
        " WHERE patient_ref = :patient_ref AND medicine_id = :medicine_id",
        patient_ref=patient_ref,
        medicine_id=medicine[0]["id"],
    )
    return evaluate_dispense(
        medicine_active=medicine[0]["is_active"],
        rules=stored_rules(connection)[medicine[0]["id"]],
        history=[PriorDispense(r["id"], r["dispensed_at"], r["quantity"]) for r in history],
        candidate=Candidate(at, quantity, authorisation_ref),
    )


def engine_disagreements(connection: Connection) -> list[str]:
    """Replay every stored dispense through the engine, oldest first, against what was stored
    before it: whatever the engine would reject, or would govern by another rule, is reported."""
    active = {row["id"]: row["is_active"] for row in rows(connection, "SELECT * FROM medicines")}
    rules = stored_rules(connection)
    history: dict[tuple[str, int], list[PriorDispense]] = {}
    problems = []
    for row in rows(connection, "SELECT * FROM dispenses ORDER BY dispensed_at, id"):
        pair = history.setdefault((row["patient_ref"], row["medicine_id"]), [])
        evaluation = evaluate_dispense(
            medicine_active=active[row["medicine_id"]],
            rules=rules[row["medicine_id"]],
            history=pair,
            candidate=Candidate(row["dispensed_at"], row["quantity"], row["authorisation_ref"]),
        )
        if not evaluation.accepted:
            codes = [violation.code for violation in evaluation.violations]
            problems.append(f"dispense {row['id']} would be rejected: {codes}")
        elif evaluation.rule is None or evaluation.rule.id != row["rule_id"]:
            problems.append(
                f"dispense {row['id']} is governed by another rule than {row['rule_id']}"
            )
        pair.append(PriorDispense(row["id"], row["dispensed_at"], row["quantity"]))
    return problems
