"""The brief's minimum dataset: 500 medicines, 3 to 5 rule periods each, and 2,000 dispenses
across at least 2,000 patient refs over two years."""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from app.domain.timeutil import to_local_date
from app.seed import TABLES, SeedSummary, run_seed
from tests.seed.support import RANDOM_SEED, TODAY, YEARS, engine_disagreements, rows

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def minimum(engine: Engine) -> Iterator[SeedSummary]:
    summary = run_seed(
        engine,
        medicines=500,
        dispenses=2000,
        patients=2000,
        years=YEARS,
        random_seed=RANDOM_SEED,
        reset=True,
        today=TODAY,
    )
    yield summary
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


def test_the_dataset_meets_every_minimum_in_the_brief(engine: Engine, minimum: SeedSummary) -> None:
    with engine.connect() as connection:
        rules_per_medicine = rows(
            connection,
            "SELECT count(r.id) AS rules FROM medicines m"
            " JOIN formulary_rules r ON r.medicine_id = m.id GROUP BY m.id",
        )
        dispenses = rows(connection, "SELECT patient_ref, dispensed_at FROM dispenses")

        assert len(rules_per_medicine) >= 500
        assert all(3 <= row["rules"] <= 5 for row in rules_per_medicine)
        assert len(dispenses) >= 2000
        assert len({row["patient_ref"] for row in dispenses}) >= 2000
        days = [to_local_date(row["dispensed_at"]) for row in dispenses]
        assert max(days) - min(days) >= timedelta(days=YEARS * 365 - 14)
        assert engine_disagreements(connection) == []
    assert minimum.ok
