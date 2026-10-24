from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from app.seed import TABLES, SeedSummary, run_seed
from tests.seed.support import DISPENSES, MEDICINES, PATIENTS, RANDOM_SEED, TODAY, YEARS


@pytest.fixture(scope="module")
def seeded(engine: Engine) -> Iterator[SeedSummary]:
    """A small dataset seeded once per module, for tests that only read it. It is removed
    afterwards: other test packages share this database and expect to find it empty."""
    summary = run_seed(
        engine,
        medicines=MEDICINES,
        dispenses=DISPENSES,
        patients=PATIENTS,
        years=YEARS,
        random_seed=RANDOM_SEED,
        reset=True,
        today=TODAY,
    )
    yield summary
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
