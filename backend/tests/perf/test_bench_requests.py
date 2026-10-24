"""How app.bench finds what to ask for, what it asks for and how it sends it, against a stand-in
for the API that serves canned pages. No server and no database."""

from collections import Counter
from collections.abc import Sequence
from random import Random
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app import bench
from app.bench import BenchError, Dataset, Medicine, Request, Scenario, Visit

MEDICINES = [Medicine(f"MED-{n:03d}", f"Drug{chr(97 + n % 26)}x{n}") for n in range(250)]
DISPENSES = [Visit(f"PT-{n % 40:02d}", MEDICINES[n % 7].code) for n in range(130)]


class FakeApi:
    """Pages of fixed rows for the two listings, canned answers for the rest. Every request that
    reaches it is recorded as (client number, path, query)."""

    def __init__(
        self, *, medicines: Sequence[Medicine] | None = None, visits: Sequence[Visit] | None = None
    ) -> None:
        self.medicines = [
            {"code": m.code, "name": m.name}
            for m in (MEDICINES if medicines is None else medicines)
        ]
        self.dispenses = [
            {"patient_ref": v.patient_ref, "medicine_code": v.medicine_code}
            for v in (DISPENSES if visits is None else visits)
        ]
        self.seen: list[tuple[int, str, dict[str, str]]] = []
        self.failing_path: str | None = None
        self.empty = False

    def client(self, number: int = 0) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            query = dict(request.url.params)
            self.seen.append((number, request.url.path, query))
            if request.url.path == self.failing_path:
                return httpx.Response(500, json={"code": "internal_error"})
            return httpx.Response(200, json=self._body(request.url.path, query))

        return httpx.Client(transport=httpx.MockTransport(handle), base_url="http://fake")

    def _body(self, path: str, query: dict[str, str]) -> dict[str, Any]:
        if self.empty:
            return {"items": [], "next_cursor": None}
        if path == "/api/v1/medicines" and "q" not in query:
            return self._page(self.medicines, query)
        if path == "/api/v1/dispenses" and not (query.keys() - {"limit", "cursor"}):
            return self._page(self.dispenses, query)
        if path.startswith("/api/v1/medicines/") and not path.endswith("/rules"):
            return {"code": "detail"}
        return {"items": [{"row": 1}], "next_cursor": None}

    @staticmethod
    def _page(rows: list[dict[str, Any]], query: dict[str, str]) -> dict[str, Any]:
        start = int(query.get("cursor", 0))
        end = start + int(query.get("limit", 25))
        return {"items": rows[start:end], "next_cursor": str(end) if end < len(rows) else None}


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


def dataset(**changes: Any) -> Dataset:
    values: dict[str, Any] = {
        "medicines": MEDICINES,
        "visits": DISPENSES,
        "medicines_complete": True,
        "visits_complete": True,
    } | changes
    return Dataset(**values)


class TestDiscover:
    def test_it_follows_the_cursors_of_both_listings_to_their_end(self, api: FakeApi) -> None:
        found = bench.discover(api.client())

        assert found == dataset()
        assert [(path, query) for _, path, query in api.seen] == [
            ("/api/v1/medicines", {"limit": "100"}),
            ("/api/v1/medicines", {"limit": "100", "cursor": "100"}),
            ("/api/v1/medicines", {"limit": "100", "cursor": "200"}),
            ("/api/v1/dispenses", {"limit": "100"}),
            ("/api/v1/dispenses", {"limit": "100", "cursor": "100"}),
        ]

    def test_a_listing_longer_than_the_page_limit_is_read_in_part_and_said_to_be(
        self, api: FakeApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bench, "DISCOVERY_PAGES", 2)

        found = bench.discover(api.client())

        assert found == dataset(
            medicines=MEDICINES[:200], visits=DISPENSES, medicines_complete=False
        )

    def test_a_server_without_dispenses_cannot_be_measured(self) -> None:
        with pytest.raises(BenchError, match="seed it first"):
            bench.discover(FakeApi(visits=[]).client())

    def test_a_server_without_medicines_cannot_be_measured(self) -> None:
        with pytest.raises(BenchError, match="seed it first"):
            bench.discover(FakeApi(medicines=[]).client())

    def test_an_error_response_names_the_request_and_the_status(self, api: FakeApi) -> None:
        api.failing_path = "/api/v1/dispenses"

        with pytest.raises(BenchError, match=r"/api/v1/dispenses\?limit=100 answered 500"):
            bench.discover(api.client())


def scenarios_of(data: Dataset) -> dict[str, Scenario]:
    return {scenario.label: scenario for scenario in bench.build_scenarios(data)}


class TestScenarios:
    def draw(self, scenario: Scenario, times: int = 300) -> list[Request]:
        rng = Random(1)
        return [scenario.draw(rng) for _ in range(times)]

    @pytest.fixture
    def scenarios(self) -> dict[str, Scenario]:
        return scenarios_of(dataset())

    def test_every_read_endpoint_and_filter_is_measured(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        assert list(scenarios) == [
            "GET /medicines",
            "GET /medicines?q=<one letter>",
            "GET /medicines?q=<5 letters of a name>",
            "GET /medicines?q=<a code>",
            "GET /medicines?q=<no match>",
            "GET /dispenses",
            "GET /dispenses?patient_ref=",
            "GET /dispenses?medicine=",
            "GET /dispenses?patient_ref=&medicine=",
            "GET /medicines/{code}/rules",
            "GET /medicines/{code}",
        ]

    def test_the_unfiltered_listings_have_no_parameters(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        medicines = self.draw(scenarios["GET /medicines"])
        assert all(r == Request("/api/v1/medicines", {}) for r in medicines)
        dispenses = self.draw(scenarios["GET /dispenses"])
        assert all(r == Request("/api/v1/dispenses", {}) for r in dispenses)

    def test_a_broad_term_is_one_letter_that_occurs_in_the_names(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        names = "".join(m.name.lower() for m in MEDICINES)
        terms = {r.params["q"] for r in self.draw(scenarios["GET /medicines?q=<one letter>"])}
        assert all(len(term) == 1 and term.isalpha() and term in names for term in terms)
        assert len(terms) > 5

    def test_a_selective_term_is_a_slice_of_a_real_name(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        names = [m.name.lower() for m in MEDICINES]
        requests = self.draw(scenarios["GET /medicines?q=<5 letters of a name>"])
        for request in requests:
            term = request.params["q"]
            assert len(term) == 5
            assert any(term in name for name in names)
        assert len({r.params["q"] for r in requests}) > 50

    def test_a_term_can_be_a_whole_code(self, scenarios: dict[str, Scenario]) -> None:
        codes = {m.code for m in MEDICINES}
        terms = {r.params["q"] for r in self.draw(scenarios["GET /medicines?q=<a code>"])}
        assert terms <= codes
        assert len(terms) > 50

    def test_a_term_that_matches_nothing_is_a_real_name_read_backwards(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        names = [m.name.lower() for m in MEDICINES]
        requests = self.draw(scenarios["GET /medicines?q=<no match>"])
        for request in requests:
            assert request.params["q"][::-1] in names
            assert not any(request.params["q"] in name for name in names)
            assert request.may_be_empty
        assert len({r.params["q"] for r in requests}) > 50

    def test_only_the_search_for_something_absent_may_be_answered_with_nothing(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        for label, scenario in scenarios.items():
            assert any(r.may_be_empty for r in self.draw(scenario, 20)) == (
                label == "GET /medicines?q=<no match>"
            )

    def test_a_name_shorter_than_the_term_is_searched_whole(self) -> None:
        short = scenarios_of(dataset(medicines=[Medicine("A-1", "Zyx")]))
        requests = self.draw(short["GET /medicines?q=<5 letters of a name>"])
        assert {r.params["q"] for r in requests} == {"zyx"}

    def test_filters_come_from_one_dispense_so_that_the_page_is_not_empty(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        pairs = {(v.patient_ref, v.medicine_code) for v in DISPENSES}
        both = self.draw(scenarios["GET /dispenses?patient_ref=&medicine="])
        assert {(r.params["patient_ref"], r.params["medicine"]) for r in both} <= pairs
        assert {r.path for r in both} == {"/api/v1/dispenses"}

        patients = self.draw(scenarios["GET /dispenses?patient_ref="])
        assert all(set(r.params) == {"patient_ref"} for r in patients)
        medicines = self.draw(scenarios["GET /dispenses?medicine="])
        assert all(set(r.params) == {"medicine"} for r in medicines)

    def test_a_medicine_with_many_dispenses_is_filtered_by_more_often(self) -> None:
        visits = [Visit(f"PT-{n}", "HOT" if n % 5 else "COLD") for n in range(100)]
        scenario = scenarios_of(dataset(visits=visits))["GET /dispenses?medicine="]

        drawn = Counter(r.params["medicine"] for r in self.draw(scenario, times=500))

        assert drawn["HOT"] > 3 * drawn["COLD"] > 0

    def test_a_code_with_a_slash_or_a_space_stays_one_path_segment(self) -> None:
        awkward = scenarios_of(dataset(medicines=[Medicine("A/B 1", "Zyx")]))
        assert {r.path for r in self.draw(awkward["GET /medicines/{code}"])} == {
            "/api/v1/medicines/A%2FB%201"
        }
        assert {r.path for r in self.draw(awkward["GET /medicines/{code}/rules"])} == {
            "/api/v1/medicines/A%2FB%201/rules"
        }

    def test_the_same_random_stream_draws_the_same_requests(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        for scenario in scenarios.values():
            assert self.draw(scenario, 50) == self.draw(scenario, 50)


def run(
    api: FakeApi, *, requests: int, warmup: int, concurrency: int, scenarios: int = 3
) -> list[bench.Stats]:
    clients = [api.client(number) for number in range(concurrency)]
    return bench.run_benchmark(
        clients,
        bench.build_scenarios(dataset())[:scenarios],
        requests=requests,
        warmup=warmup,
        rng=Random(1),
    )


class TestRunBenchmark:
    def test_it_sends_the_warm_up_and_counts_only_the_rest(self, api: FakeApi) -> None:
        stats = run(api, requests=20, warmup=5, concurrency=1)

        assert [row.n for row in stats] == [20, 20, 20]
        assert len(api.seen) == 3 * (5 + 20)

    def test_every_worker_warms_up_and_then_sends_its_share_of_the_requests(
        self, api: FakeApi
    ) -> None:
        stats = run(api, requests=10, warmup=4, concurrency=3, scenarios=1)

        assert stats[0].n == 10
        by_worker = Counter(number for number, _, _ in api.seen)
        # Four warm-up requests each, then ten requests dealt out as 4, 3 and 3.
        assert sorted(by_worker.values()) == [7, 7, 8]
        assert sum(by_worker.values()) == 3 * 4 + 10

    def test_it_reports_each_scenario_under_its_label(self, api: FakeApi) -> None:
        stats = run(api, requests=3, warmup=0, concurrency=1, scenarios=2)
        assert [row.endpoint for row in stats] == [
            "GET /medicines",
            "GET /medicines?q=<one letter>",
        ]

    def test_a_latency_is_the_time_around_the_request_alone(
        self, api: FakeApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two readings per request: before it is sent and when its answer is complete.
        readings = iter([0.0, 0.0125, 5.0, 5.003, 9.0, 9.2])
        monkeypatch.setattr(bench, "time", SimpleNamespace(perf_counter=lambda: next(readings)))

        stats = run(api, requests=3, warmup=0, concurrency=1, scenarios=1)

        assert (stats[0].min, stats[0].p50, stats[0].max) == pytest.approx((3.0, 12.5, 200.0))

    def test_an_error_response_on_any_worker_stops_the_run_instead_of_being_counted(
        self, api: FakeApi
    ) -> None:
        api.failing_path = "/api/v1/medicines"
        with pytest.raises(BenchError, match="answered 500"):
            run(api, requests=8, warmup=0, concurrency=4)

    def test_a_search_for_something_absent_may_be_answered_with_an_empty_page(
        self, api: FakeApi
    ) -> None:
        api.empty = True
        absent = Scenario("absent", lambda rng: Request("/api/v1/medicines", {"q": "zz"}, True))

        stats = bench.run_benchmark([api.client()], [absent], requests=3, warmup=0, rng=Random(1))

        assert stats[0].n == 3

    def test_an_empty_page_is_an_error_not_a_fast_answer(self, api: FakeApi) -> None:
        api.empty = True
        with pytest.raises(BenchError, match="empty page"):
            run(api, requests=2, warmup=0, concurrency=1)
