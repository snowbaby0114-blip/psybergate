"""The seed command: re-runs, reset, determinism, atomicity and the command line."""

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError

from app import seed
from app.config import get_settings
from app.seed import (
    TABLES,
    DatabaseNotEmpty,
    Expected,
    SeedSummary,
    Verification,
    main,
    run_seed,
)
from tests.seed.support import RANDOM_SEED, TODAY, YEARS, dump

BACKEND = Path(__file__).resolve().parents[2]
SEED_IN_A_NEW_PROCESS = """
from datetime import date
from app.db import create_db_engine
from app.seed import run_seed

run_seed(
    create_db_engine(), medicines=30, dispenses=60, patients=30, years=2,
    random_seed=7, reset=True, today=date(2026, 6, 15),
)
"""

MEDICINES = 30
DISPENSES = 60
PATIENTS = 30
DEMO_MEDICINES = 4
DEMO_DISPENSES = 10

# Single rows that make a database "not empty" without any of the rows around them.
LONE_MEDICINE = (
    "INSERT INTO medicines (code, name, form, strength_value, strength_unit)"
    " VALUES ('X-1', 'Name', 'tablet', 5, 'mg')"
)
LONE_REJECTED_ATTEMPT = (
    "INSERT INTO dispense_attempts (idempotency_key, request_hash, outcome, medicine_code,"
    " patient_ref, quantity, dispensed_at, violations)"
    " VALUES ('k', 'h', 'rejected', 'X', 'PT-1', 1, now(), '[{\"code\": \"x\"}]'::jsonb)"
)


@pytest.fixture(autouse=True, scope="module")
def _leave_database_empty(engine: Engine) -> Iterator[None]:
    """Other test packages share this database and expect to find it empty."""
    yield
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


def seed_small(
    engine: Engine, *, random_seed: int = RANDOM_SEED, reset: bool = True, **overrides: Any
) -> SeedSummary:
    parameters: dict[str, Any] = {
        "medicines": MEDICINES,
        "dispenses": DISPENSES,
        "patients": PATIENTS,
        "years": YEARS,
        "today": TODAY,
    } | overrides
    return run_seed(engine, random_seed=random_seed, reset=reset, **parameters)


def break_self_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the first check of the self-check fail, whatever the data."""
    real = seed.verify

    def verify_and_break(connection: Connection, expected: Expected) -> Verification:
        verification = real(connection, expected)
        broken = replace(verification.checks[0], ok=False, detail="offending rows: 7")
        return replace(verification, checks=(broken, *verification.checks[1:]))

    monkeypatch.setattr(seed, "verify", verify_and_break)


def count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


class TestRerunsAndReset:
    def test_an_empty_database_is_seeded_without_reset(
        self, engine: Engine, clean_db: None
    ) -> None:
        summary = seed_small(engine, reset=False)

        assert count(engine, "medicines") == MEDICINES + DEMO_MEDICINES
        assert count(engine, "dispenses") == DISPENSES + DEMO_DISPENSES
        assert summary.expected.dispenses == DISPENSES + DEMO_DISPENSES

    def test_a_database_holding_data_is_left_untouched_without_reset(
        self, engine: Engine, clean_db: None
    ) -> None:
        seed_small(engine)
        before = dump(engine)

        with pytest.raises(DatabaseNotEmpty, match="--reset"):
            seed_small(engine, random_seed=RANDOM_SEED + 1, reset=False)

        assert dump(engine) == before

    @pytest.mark.parametrize(
        "statement", [LONE_MEDICINE, LONE_REJECTED_ATTEMPT], ids=["medicine", "rejected attempt"]
    )
    def test_data_in_any_table_counts_as_not_empty(
        self, engine: Engine, clean_db: None, statement: str
    ) -> None:
        with engine.begin() as connection:
            connection.execute(text(statement))
        before = dump(engine)

        with pytest.raises(DatabaseNotEmpty):
            seed_small(engine, reset=False)

        assert dump(engine) == before

    def test_reset_replaces_everything_and_restarts_the_identities(
        self, engine: Engine, clean_db: None
    ) -> None:
        seed_small(engine, random_seed=2)
        fresh = dump(engine)

        seed_small(engine, random_seed=1)
        assert dump(engine) != fresh
        seed_small(engine, random_seed=2)

        assert dump(engine) == fresh
        for table, stored in fresh.items():
            assert stored[0]["id"] == 1, table

    def test_a_failure_part_way_leaves_the_previous_data_in_place(
        self, engine: Engine, clean_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_small(engine)
        before = dump(engine)

        def fail(*args: object, **kwargs: object) -> None:
            raise RuntimeError("the connection dropped")

        monkeypatch.setattr(seed, "_insert_dispenses", fail)
        with pytest.raises(RuntimeError, match="dropped"):
            seed_small(engine, random_seed=RANDOM_SEED + 1)

        # The truncate, the catalogue and every dispense are one transaction.
        assert dump(engine) == before


class TestFailedSelfCheck:
    def test_nothing_is_saved_in_an_empty_database(
        self, engine: Engine, clean_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        break_self_check(monkeypatch)

        summary = seed_small(engine, reset=False)

        assert not summary.ok
        assert [count(engine, table) for table in TABLES] == [0, 0, 0, 0]

    def test_a_reset_whose_check_fails_keeps_the_previous_data(
        self, engine: Engine, clean_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_small(engine)
        before = dump(engine)
        break_self_check(monkeypatch)

        summary = seed_small(engine, random_seed=RANDOM_SEED + 1)

        assert not summary.ok
        assert dump(engine) == before


class TestDeterminism:
    def test_the_same_seed_and_day_give_identical_rows_ids_and_timestamps(
        self, engine: Engine, clean_db: None
    ) -> None:
        seed_small(engine)
        first = dump(engine)
        seed_small(engine)

        assert dump(engine) == first

    @pytest.mark.slow
    def test_the_data_does_not_depend_on_the_interpreters_hash_seed(
        self, engine: Engine, database_url: str, clean_db: None
    ) -> None:
        # Each process hashes strings differently, so any iteration over a set of strings, or
        # any other hash-ordered step, would show up as different rows.
        dumps = []
        for hash_seed in ("1", "4242"):
            environment = os.environ | {
                "PYTHONHASHSEED": hash_seed,
                "DATABASE_URL": database_url,
                "PYTHONPATH": str(BACKEND),
            }
            subprocess.run(
                [sys.executable, "-c", SEED_IN_A_NEW_PROCESS],
                cwd=BACKEND,
                env=environment,
                check=True,
            )
            dumps.append(dump(engine))

        assert dumps[0] == dumps[1]
        assert dumps[0]["dispenses"]

    def test_another_seed_or_another_day_gives_other_data(
        self, engine: Engine, clean_db: None
    ) -> None:
        seed_small(engine)
        base = dump(engine)

        seed_small(engine, random_seed=RANDOM_SEED + 1)
        other_seed = dump(engine)
        seed_small(engine, today=TODAY + timedelta(days=1))
        other_day = dump(engine)

        assert other_seed["dispenses"] != base["dispenses"]
        assert other_seed["medicines"] != base["medicines"]
        assert other_day["dispenses"] != base["dispenses"]


class TestParameters:
    @pytest.mark.parametrize(
        ("overrides", "complaint"),
        [
            ({"medicines": 0}, "--medicines"),
            ({"patients": 0, "dispenses": 0}, "--patients"),
            ({"years": 0}, "--years"),
            ({"years": 26}, "--years"),
            ({"dispenses": PATIENTS - 1}, "at least --patients"),
            ({"dispenses": PATIENTS * 40 + 1}, "use more patients"),
        ],
    )
    def test_impossible_requests_are_refused_before_touching_the_database(
        self, engine: Engine, clean_db: None, overrides: dict[str, int], complaint: str
    ) -> None:
        with pytest.raises(ValueError, match=complaint):
            seed_small(engine, **overrides)

        assert count(engine, "medicines") == 0

    def test_the_limits_themselves_are_allowed(self, engine: Engine, clean_db: None) -> None:
        summary = seed_small(engine, dispenses=PATIENTS)
        assert summary.expected.dispenses == PATIENTS + DEMO_DISPENSES

        summary = seed_small(engine, years=25, dispenses=PATIENTS * 40)
        assert summary.expected.dispenses == PATIENTS * 40 + DEMO_DISPENSES


@pytest.fixture
def command_line(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the settings, which main() reads for itself, at the test database."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


SMALL = ["--medicines", "30", "--dispenses", "60", "--patients", "30"]


@pytest.mark.usefixtures("command_line", "clean_db")
class TestCommandLine:
    def test_it_seeds_prints_a_summary_and_exits_zero(
        self, engine: Engine, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(SMALL) == 0

        out = capsys.readouterr().out
        assert "Seeded with random seed 20260901" in out
        for fixture in ("DEMO-AUTH", "DEMO-TIGHT", "DEMO-INACTIVE", "DEMO-HISTORY", "PT-DEMO"):
            assert fixture in out
        assert count(engine, "medicines") == MEDICINES + DEMO_MEDICINES

    def test_running_it_again_says_so_in_one_line_and_changes_nothing(
        self, engine: Engine, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(SMALL) == 0
        capsys.readouterr()
        before = dump(engine)

        assert main(SMALL) == 0

        out = capsys.readouterr().out
        assert len(out.strip().splitlines()) == 1
        assert "--reset" in out
        assert dump(engine) == before

    def test_reset_seeds_again_with_the_new_arguments(self, engine: Engine) -> None:
        assert main(SMALL) == 0

        assert main(["--medicines", "40", "--dispenses", "80", "--patients", "40", "--reset"]) == 0

        assert count(engine, "medicines") == 40 + DEMO_MEDICINES
        assert count(engine, "dispenses") == 80 + DEMO_DISPENSES

    @pytest.mark.parametrize(
        "arguments",
        [["--dispenses", "5", "--patients", "10"], ["--years", "x"], ["--unknown"]],
    )
    def test_bad_arguments_exit_with_status_two_and_touch_nothing(
        self, engine: Engine, capsys: pytest.CaptureFixture[str], arguments: list[str]
    ) -> None:
        with pytest.raises(SystemExit) as raised:
            main(arguments)

        assert raised.value.code == 2
        assert "error" in capsys.readouterr().err
        assert count(engine, "medicines") == 0

    def test_a_failed_self_check_is_named_saves_nothing_and_exits_one(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        break_self_check(monkeypatch)

        assert main(SMALL) == 1

        out = capsys.readouterr().out
        assert "Self-check FAILED" in out
        assert "nothing was saved" in out
        assert "every medicine has 3 to 5 rule periods: offending rows: 7" in out
        assert count(engine, "medicines") == 0

    def test_a_database_without_tables_is_told_to_run_the_migrations(
        self,
        scratch_database: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", scratch_database)
        get_settings.cache_clear()

        assert main(SMALL) == 1

        assert "alembic upgrade head" in capsys.readouterr().err

    def test_an_unreachable_database_is_reported_and_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def unreachable(*args: object, **kwargs: object) -> None:
            raise OperationalError("SELECT 1", {}, ConnectionError("connection refused"))

        monkeypatch.setattr(seed, "run_seed", unreachable)

        assert main(SMALL) == 1

        err = capsys.readouterr().err
        assert "Cannot use the database" in err
        assert "connection refused" in err
