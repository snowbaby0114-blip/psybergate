"""`python -m app.bench` end to end: the command line, a real HTTP server, a real database."""

import socket
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from app import bench
from app.seed import SeedSummary
from tests.perf.dataset import seeded_database

# Long enough for any machine that runs the tests: what these tests check is the command, not the
# speed of the server.
LENIENT_BUDGET = "60000"
SMALL = ["--requests", "20", "--warmup", "5"]


@pytest.fixture(scope="class")
def seeded(engine: Engine) -> Iterator[SeedSummary]:
    with seeded_database(engine, medicines=60, dispenses=300, patients=150) as summary:
        yield summary


class TestAgainstASeededServer:
    @pytest.mark.usefixtures("seeded")
    def test_it_measures_every_endpoint_and_succeeds_within_budget(
        self, live_server: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = bench.main(["--base-url", live_server, *SMALL, "--budget-ms", LENIENT_BUDGET])

        report = capsys.readouterr().out
        assert status == 0
        assert report.count("| 20 |") == 11
        assert "Within budget" in report

    def test_it_reports_the_size_of_the_dataset_it_read(
        self, live_server: str, seeded: SeedSummary, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stats = seeded.verification.stats

        bench.main(["--base-url", live_server, *SMALL, "--budget-ms", LENIENT_BUDGET])

        assert (
            f"Dataset seen: {stats.medicines:,} medicines, {stats.dispenses:,} dispenses."
            in capsys.readouterr().out
        )

    @pytest.mark.usefixtures("seeded")
    def test_it_fails_when_a_p95_reaches_the_budget(
        self, live_server: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = bench.main(["--base-url", live_server, *SMALL, "--budget-ms", "0.001"])

        assert status == 1
        assert "OVER BUDGET" in capsys.readouterr().out

    @pytest.mark.usefixtures("seeded")
    def test_workers_share_the_requests_and_the_count_stays_the_same(
        self, live_server: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = bench.main(
            ["--base-url", live_server, *SMALL, "--concurrency", "4", "--budget-ms", LENIENT_BUDGET]
        )

        report = capsys.readouterr().out
        assert status == 0
        assert "4 workers" in report
        assert "| `GET /dispenses` | 20 |" in report


class TestAgainstAnEmptyServer:
    @pytest.mark.usefixtures("clean_db")
    def test_there_is_nothing_to_measure_so_it_says_how_to_seed(
        self, live_server: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = bench.main(["--base-url", live_server, *SMALL])

        captured = capsys.readouterr()
        assert status == 2
        assert "python -m app.seed" in captured.err
        assert captured.out == ""


class TestWithoutAServer:
    def test_an_unreachable_server_is_reported_and_is_not_a_slow_run(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]

        status = bench.main(["--base-url", f"http://127.0.0.1:{port}", *SMALL])

        captured = capsys.readouterr()
        assert status == 2
        assert f"http://127.0.0.1:{port}" in captured.err
        assert captured.out == ""

    @pytest.mark.parametrize(
        "arguments",
        [
            ["--requests", "0"],
            ["--concurrency", "0"],
            ["--warmup", "-1"],
            ["--budget-ms", "0"],
        ],
    )
    def test_numbers_that_cannot_make_a_benchmark_are_refused(self, arguments: list[str]) -> None:
        with pytest.raises(SystemExit) as refused:
            bench.main(arguments)
        assert refused.value.code == 2
