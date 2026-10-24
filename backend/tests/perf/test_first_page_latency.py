"""The brief's budget, checked in process on the larger dataset of the query-plan tests: 153,000
medicines and 30,000 dispenses, ten times the brief's volume for dispenses and three hundred times
for medicines.

This is a smoke test. It sends requests straight into the application, with no socket in between,
so it cannot stand in for `python -m app.bench` against a running server, which is how the
contract is measured; what it can do is fail whenever a change makes a first page slow, on every
run of the test suite.
"""

import warnings
from collections.abc import Iterator
from random import Random

import pytest
from sqlalchemy import Engine
from starlette.exceptions import StarletteDeprecationWarning

from app import bench
from app.config import Settings
from app.main import create_app
from tests.perf.dataset import large_dataset

with warnings.catch_warnings():
    # Starlette's test client now prefers httpx2 and warns when it finds the httpx this project
    # pins; the warning is about the dependency, not about anything the tests do.
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

# Building the dataset and sending 2,400 requests takes about 50 s here.
pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]

REQUESTS = 200
WARMUP = 20


@pytest.fixture(scope="module")
def client(engine: Engine, database_url: str) -> Iterator[TestClient]:
    with large_dataset(engine):
        app = create_app(engine=engine, settings=Settings(database_url=database_url))
        with TestClient(app) as test_client:
            yield test_client


def test_every_endpoint_answers_its_first_page_within_the_budget(client: TestClient) -> None:
    dataset = bench.discover(client)

    stats = bench.run_benchmark(
        [client],
        bench.build_scenarios(dataset),
        requests=REQUESTS,
        warmup=WARMUP,
        rng=Random(bench.RANDOM_SEED),
    )

    print(f"\n{bench.format_table(stats)}")
    assert [row.n for row in stats] == [REQUESTS] * len(stats)
    assert bench.over_budget(stats, bench.DEFAULT_BUDGET_MS) == []
