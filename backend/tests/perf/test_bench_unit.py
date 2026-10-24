"""The statistics and the report of app.bench, with no server and no database."""

import pytest

from app.bench import (
    Dataset,
    Medicine,
    Stats,
    Visit,
    format_report,
    format_table,
    format_verdict,
    over_budget,
    percentile,
    summarise,
)


class TestPercentile:
    @pytest.mark.parametrize(
        ("pct", "expected"), [(1, 1), (50, 50), (95, 95), (99, 99), (100, 100)]
    )
    def test_the_p_th_of_a_hundred_is_the_p_th_smallest(self, pct: int, expected: int) -> None:
        assert percentile(range(1, 101), pct) == expected

    def test_with_300_requests_the_p95_is_the_285th_fastest(self) -> None:
        assert percentile(range(1, 301), 95) == 285
        assert percentile(range(1, 301), 99) == 297

    @pytest.mark.parametrize(
        ("pct", "expected"),
        [
            (25, 10),  # ceil(0.25 * 4) = 1st
            (50, 20),  # ceil(0.50 * 4) = 2nd: no averaging of the middle two
            (51, 30),  # ceil(0.51 * 4) = 3rd
            (95, 40),  # ceil(0.95 * 4) = 4th
        ],
    )
    def test_the_rank_is_rounded_up_and_never_interpolated(self, pct: int, expected: int) -> None:
        assert percentile([10, 20, 30, 40], pct) == expected

    def test_the_result_is_always_one_of_the_samples(self) -> None:
        samples = [3.5, 120.25, 7.0, 9.75, 4.0, 88.5, 5.5]
        assert all(percentile(samples, pct) in samples for pct in range(1, 101))

    def test_the_order_of_the_samples_does_not_matter(self) -> None:
        shuffled = [5, 1, 4, 2, 3]
        assert percentile(shuffled, 50) == 3
        assert shuffled == [5, 1, 4, 2, 3]

    @pytest.mark.parametrize("pct", [1, 50, 95, 100])
    def test_one_sample_is_every_percentile(self, pct: int) -> None:
        assert percentile([42.0], pct) == 42.0

    def test_no_samples_have_no_percentile(self) -> None:
        with pytest.raises(ValueError, match="no samples"):
            percentile([], 95)

    @pytest.mark.parametrize("pct", [0, -5, 101])
    def test_a_percentile_outside_1_to_100_is_refused(self, pct: int) -> None:
        with pytest.raises(ValueError, match="between 1 and 100"):
            percentile([1.0], pct)


class TestSummarise:
    def test_it_reports_the_extremes_and_three_percentiles(self) -> None:
        stats = summarise("GET /x", [float(ms) for ms in range(300, 0, -1)])
        assert stats == Stats("GET /x", n=300, min=1.0, p50=150.0, p95=285.0, p99=297.0, max=300.0)

    def test_one_slow_request_in_a_hundred_is_the_max_but_not_the_p99(self) -> None:
        stats = summarise("GET /x", [10.0] * 99 + [900.0])
        assert (stats.p99, stats.max) == (10.0, 900.0)

    def test_without_measurements_there_is_nothing_to_summarise(self) -> None:
        with pytest.raises(ValueError):
            summarise("GET /x", [])


def stats(endpoint: str, p95: float) -> Stats:
    return Stats(endpoint, n=300, min=1.0, p50=2.0, p95=p95, p99=p95 + 1, max=p95 + 2)


class TestFormatTable:
    def test_it_is_a_markdown_table_with_one_decimal_per_figure(self) -> None:
        rows = [
            Stats("GET /medicines", 300, min=2.04, p50=3.25, p95=7.5, p99=12.34, max=40.0),
            Stats("GET /dispenses", 150, min=1.0, p50=2.0, p95=3.0, p99=4.0, max=5.0),
        ]
        assert format_table(rows).splitlines() == [
            "| Endpoint | n | min (ms) | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) |",
            "|---|---:|---:|---:|---:|---:|---:|",
            "| `GET /medicines` | 300 | 2.0 | 3.2 | 7.5 | 12.3 | 40.0 |",
            "| `GET /dispenses` | 150 | 1.0 | 2.0 | 3.0 | 4.0 | 5.0 |",
        ]


class TestBudget:
    def test_a_p95_equal_to_the_budget_is_over_it(self) -> None:
        rows = [stats("fast", 299.9), stats("edge", 300.0), stats("slow", 450.0)]
        assert [row.endpoint for row in over_budget(rows, 300)] == ["edge", "slow"]

    def test_no_row_is_over_a_budget_they_all_beat(self) -> None:
        assert over_budget([stats("a", 12.0), stats("b", 40.0)], 300) == []

    def test_the_verdict_names_the_slowest_endpoint_when_all_are_within_budget(self) -> None:
        verdict = format_verdict([stats("a", 12.0), stats("b", 40.5)], 300)
        assert verdict == "Within budget: every p95 is below 300 ms; the slowest is 40.5 ms (b)."

    def test_the_verdict_names_every_endpoint_over_budget(self) -> None:
        verdict = format_verdict([stats("a", 12.0), stats("b", 310.0), stats("c", 301.25)], 300)
        assert verdict == "OVER BUDGET: p95 is not below 300 ms for b (310.0 ms), c (301.2 ms)."


class TestFormatReport:
    def report(self, *, workers: int = 1, **changes: bool) -> str:
        dataset = Dataset(
            medicines=[Medicine("A", "Alpha")] * 1500,
            visits=[Visit("P", "A")] * 2000,
            **{"medicines_complete": True, "visits_complete": True} | changes,
        )
        return format_report(
            [stats("GET /x", 7.0)],
            dataset,
            base_url="http://api:8000",
            requests=300,
            warmup=30,
            concurrency=workers,
            budget_ms=300,
        )

    def test_it_says_what_was_measured_where_and_how(self) -> None:
        lines = self.report().splitlines()

        assert lines[0] == "## First-page latency of http://api:8000"
        assert lines[2] == "Dataset seen: 1,500 medicines, 2,000 dispenses."
        assert lines[3] == (
            "300 measured requests per endpoint after 30 warm-up requests per worker, 1 worker "
            "on keep-alive connections. Nearest-rank percentiles; budget: p95 below 300 ms."
        )

    def test_it_ends_with_the_table_and_the_verdict(self) -> None:
        lines = self.report().splitlines()

        assert lines[5:-2] == format_table([stats("GET /x", 7.0)]).splitlines()
        assert lines[-1] == format_verdict([stats("GET /x", 7.0)], 300)

    def test_a_listing_that_was_read_in_part_gives_a_lower_bound(self) -> None:
        line = self.report(visits_complete=False).splitlines()[2]
        assert line == "Dataset seen: 1,500 medicines, at least 2,000 dispenses."

    def test_workers_are_in_the_plural(self) -> None:
        assert "8 workers on keep-alive" in self.report(workers=8)
