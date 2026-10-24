"""The datasets the performance tests build, with the seed command and a bulk load."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from random import Random

from sqlalchemy import Connection, Engine, text

from app.seed import TABLES, SeedSummary, run_seed

RANDOM_SEED = 20260901
# The history ends on this date whatever day the tests run, so the data is always the same.
TODAY = date(2026, 6, 15)

# A catalogue of this size makes PostgreSQL choose the trigram indexes for a selective search
# (measured: it prefers a sequential scan up to about 90,000 medicines, and the index is the
# faster plan from far below that); a test of the plan needs a table on the far side of it.
BULK_MEDICINES = 150_000
SEEDED_MEDICINES = 3_000
SEEDED_DISPENSES = 30_000
SEEDED_PATIENTS = 12_000


@contextmanager
def seeded_database(
    engine: Engine, *, medicines: int, dispenses: int, patients: int
) -> Iterator[SeedSummary]:
    """A dataset made by the seed command, removed afterwards: other test packages share this
    database and expect to find it empty."""
    summary = run_seed(
        engine,
        medicines=medicines,
        dispenses=dispenses,
        patients=patients,
        years=2,
        random_seed=RANDOM_SEED,
        reset=True,
        today=TODAY,
    )
    try:
        yield summary
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


_SYLLABLES = [c + v for c in "bcdfghjklmnprstvwz" for v in "aeiou"]


def _add_medicines(engine: Engine, count: int) -> list[str]:
    """Insert `count` medicines with invented, practically unique names, directly by COPY, and
    return their names. Each gets one open-ended rule, so that every endpoint has an answer for
    it; none has dispenses."""
    rng = Random(RANDOM_SEED)
    names = [
        "".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(3, 5))).capitalize()
        for _ in range(count)
    ]
    connection = engine.raw_connection()
    try:
        with (
            connection.cursor() as cursor,
            cursor.copy(
                "COPY medicines (code, name, form, strength_value, strength_unit) FROM STDIN"
            ) as copy,
        ):
            for number, name in enumerate(names, start=100_001):
                copy.write_row((f"MED-{number:06d}", name, "tablet", 100, "mg"))
        connection.commit()
    finally:
        connection.close()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO formulary_rules (medicine_id, effective_from, "
                "max_quantity_per_dispense, max_quantity_per_30_days) "
                "SELECT id, DATE '2024-01-01', 30, 60 FROM medicines WHERE code >= 'MED-100001'"
            )
        )
    return names


@dataclass(frozen=True)
class Samples:
    """Values to run the queries with, picked from the data so that each is typical of its kind:
    the busiest medicine and patient, a position half way down each listing, and search terms of
    each selectivity."""

    medicine_id: int
    medicine_code: str
    patient_ref: str
    # A patient and a medicine that were dispensed together most often, and the last such time.
    pair_patient_ref: str
    pair_medicine_id: int
    pair_last_dispensed_at: datetime
    medicine_cursor: tuple[str, str]
    dispense_cursor: tuple[datetime, int]
    attempt_key: str
    rare_name_part: str
    rare_code: str
    medicines: int
    dispenses: int
    patients: int


def _rare_name_part(connection: Connection, names: Sequence[str]) -> str:
    """Five letters from inside one of the names that few medicines contain."""
    for name in names:
        term = name[1:6].lower()
        matches = connection.execute(
            text("SELECT count(*) FROM medicines WHERE name ILIKE :p OR code ILIKE :p"),
            {"p": f"%{term}%"},
        ).scalar_one()
        if 1 <= matches <= 20:
            return term
    raise AssertionError("no selective search term among the generated names")


def _sample(connection: Connection, names: Sequence[str]) -> Samples:
    medicine = connection.execute(
        text(
            "SELECT m.id, m.code FROM dispenses d JOIN medicines m ON m.id = d.medicine_id "
            "GROUP BY m.id, m.code ORDER BY count(*) DESC, m.id LIMIT 1"
        )
    ).one()
    patient = connection.execute(
        text(
            "SELECT patient_ref FROM dispenses GROUP BY patient_ref "
            "ORDER BY count(*) DESC, patient_ref LIMIT 1"
        )
    ).scalar_one()
    pair = connection.execute(
        text(
            "SELECT patient_ref, medicine_id, max(dispensed_at) FROM dispenses "
            "GROUP BY patient_ref, medicine_id ORDER BY count(*) DESC, 1, 2 LIMIT 1"
        )
    ).one()
    medicine_cursor = connection.execute(
        text(
            "SELECT name, code FROM medicines ORDER BY name, code "
            "OFFSET (SELECT count(*) / 2 FROM medicines) LIMIT 1"
        )
    ).one()
    dispense_cursor = connection.execute(
        text(
            "SELECT dispensed_at, id FROM dispenses ORDER BY dispensed_at DESC, id DESC "
            "OFFSET (SELECT count(*) / 2 FROM dispenses) LIMIT 1"
        )
    ).one()
    medicines, dispenses, patients, attempt_key = connection.execute(
        text(
            "SELECT (SELECT count(*) FROM medicines), (SELECT count(*) FROM dispenses), "
            "(SELECT count(DISTINCT patient_ref) FROM dispenses), "
            "(SELECT idempotency_key FROM dispense_attempts ORDER BY id LIMIT 1)"
        )
    ).one()
    return Samples(
        medicine_id=medicine.id,
        medicine_code=medicine.code,
        patient_ref=patient,
        pair_patient_ref=pair[0],
        pair_medicine_id=pair[1],
        pair_last_dispensed_at=pair[2],
        medicine_cursor=(medicine_cursor[0], medicine_cursor[1]),
        dispense_cursor=(dispense_cursor[0], dispense_cursor[1]),
        attempt_key=attempt_key,
        rare_name_part=_rare_name_part(connection, names),
        rare_code=f"MED-{100_001 + len(names) // 2:06d}",
        medicines=medicines,
        dispenses=dispenses,
        patients=patients,
    )


@contextmanager
def large_dataset(engine: Engine) -> Iterator[Samples]:
    """The seeded dataset plus a bulk catalogue, analysed, with the samples to query it by."""
    with seeded_database(
        engine, medicines=SEEDED_MEDICINES, dispenses=SEEDED_DISPENSES, patients=SEEDED_PATIENTS
    ):
        names = _add_medicines(engine, BULK_MEDICINES)
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text("ANALYZE medicines"))
        with engine.connect() as connection:
            samples = _sample(connection, names)
        yield samples
