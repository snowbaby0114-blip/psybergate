"""First-page latency of every read endpoint, measured over HTTP against a running server.

    python -m app.bench [--base-url http://localhost:8000] [--requests 300] [--warmup 30]
                        [--concurrency 1] [--budget-ms 300]

Only HTTP is used, so it runs unchanged against a server on the host or inside the container
(`docker compose exec api python -m app.bench`), and it needs no database access: the parameters
of its requests (medicine codes, patient refs, search terms) are read from the server's own
listings. Each endpoint is measured on its first page, with parameters that vary from request to
request, over keep-alive connections: `--concurrency` workers each keep one open and send their
next request as soon as the previous one is answered. Latency is the time from sending the request
to receiving the whole response, in milliseconds.

Percentiles use the nearest-rank method: the p-th percentile of n measurements is the
ceil(p / 100 * n)-th smallest one. It is always a latency that really happened, never an
interpolation: with 300 requests the p95 is the 285th-fastest, and only 15 were slower.

Exit status: 0 when every p95 is below the budget, 1 when one is not, 2 when the benchmark could
not be run (server unreachable, an error response, nothing to measure).
"""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from random import Random
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx

API_PREFIX = "/api/v1"
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_REQUESTS = 300
DEFAULT_WARMUP = 30
DEFAULT_BUDGET_MS = 300
# The same parameters on every run, so two runs differ only in the server's answers.
RANDOM_SEED = 20260901
TIMEOUT_SECONDS = 30
# Discovery reads listings a page of the largest size at a time, and stops after this many pages:
# the sample then holds the first 10,000 rows of the listing, which is plenty to draw from.
PAGE_SIZE = 100
DISCOVERY_PAGES = 100
SELECTIVE_TERM_LENGTH = 5


class BenchError(Exception):
    """The benchmark cannot run, or an answer proves that its numbers would mean nothing."""


def percentile(samples: Sequence[float], pct: int) -> float:
    """Nearest-rank percentile. `pct` is a whole number in 1..100, so the rank is computed
    in integers and no float rounding can move it by one."""
    if not samples:
        raise ValueError("no samples")
    if not 1 <= pct <= 100:
        raise ValueError(f"percentile must be between 1 and 100, not {pct}")
    rank = (pct * len(samples) + 99) // 100
    return sorted(samples)[rank - 1]


@dataclass(frozen=True)
class Stats:
    """Latencies of one endpoint, in milliseconds."""

    endpoint: str
    n: int
    min: float
    p50: float
    p95: float
    p99: float
    max: float


def summarise(endpoint: str, latencies_ms: Sequence[float]) -> Stats:
    return Stats(
        endpoint=endpoint,
        n=len(latencies_ms),
        min=min(latencies_ms),
        p50=percentile(latencies_ms, 50),
        p95=percentile(latencies_ms, 95),
        p99=percentile(latencies_ms, 99),
        max=max(latencies_ms),
    )


def format_table(stats: Sequence[Stats]) -> str:
    lines = [
        "| Endpoint | n | min (ms) | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in stats:
        figures = " | ".join(f"{value:.1f}" for value in (row.min, row.p50, row.p95, row.p99))
        lines.append(f"| `{row.endpoint}` | {row.n} | {figures} | {row.max:.1f} |")
    return "\n".join(lines)


def over_budget(stats: Sequence[Stats], budget_ms: float) -> list[Stats]:
    return [row for row in stats if row.p95 >= budget_ms]


def format_verdict(stats: Sequence[Stats], budget_ms: float) -> str:
    over = over_budget(stats, budget_ms)
    if over:
        names = ", ".join(f"{row.endpoint} ({row.p95:.1f} ms)" for row in over)
        return f"OVER BUDGET: p95 is not below {budget_ms:g} ms for {names}."
    worst = max(stats, key=lambda row: row.p95)
    return (
        f"Within budget: every p95 is below {budget_ms:g} ms; "
        f"the slowest is {worst.p95:.1f} ms ({worst.endpoint})."
    )


class Medicine(NamedTuple):
    code: str
    name: str


class Visit(NamedTuple):
    """A dispense as the listing shows it: the filters that find it again."""

    patient_ref: str
    medicine_code: str


@dataclass(frozen=True)
class Dataset:
    """What the server holds, as far as the benchmark read it. A listing that was not read to
    its end gives a lower bound on the size, not the size."""

    medicines: list[Medicine]
    visits: list[Visit]
    medicines_complete: bool
    visits_complete: bool


class Request(NamedTuple):
    path: str
    params: dict[str, str]
    # Only a search for something that is not there may be answered with an empty page.
    may_be_empty: bool = False


@dataclass(frozen=True)
class Scenario:
    """An endpoint to measure, and how to draw the parameters of its next request."""

    label: str
    draw: Callable[[Random], Request]


def _ensure_ok(response: httpx.Response) -> None:
    if response.status_code != 200:
        raise BenchError(
            f"GET {response.request.url} answered {response.status_code}: {response.text[:200]}"
        )


def _read_listing(client: httpx.Client, path: str, pages: int) -> tuple[list[dict[str, Any]], bool]:
    """The rows of the first `pages` pages of a listing, and whether that was all of it."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(pages):
        params = {"limit": str(PAGE_SIZE)} | ({"cursor": cursor} if cursor else {})
        response = client.get(f"{API_PREFIX}{path}", params=params)
        _ensure_ok(response)
        body = response.json()
        rows += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            return rows, True
    return rows, False


def discover(client: httpx.Client) -> Dataset:
    medicines, medicines_complete = _read_listing(client, "/medicines", DISCOVERY_PAGES)
    dispenses, visits_complete = _read_listing(client, "/dispenses", DISCOVERY_PAGES)
    if not medicines or not dispenses:
        raise BenchError(
            "The server holds no medicines or no dispenses to draw requests from; "
            "seed it first (python -m app.seed)."
        )
    return Dataset(
        medicines=[Medicine(row["code"], row["name"]) for row in medicines],
        visits=[Visit(row["patient_ref"], row["medicine_code"]) for row in dispenses],
        medicines_complete=medicines_complete,
        visits_complete=visits_complete,
    )


def build_scenarios(dataset: Dataset) -> list[Scenario]:
    medicines, visits = dataset.medicines, dataset.visits
    # Letters are drawn from every name, so a letter is as likely as it is common in the names.
    letters = [char for medicine in medicines for char in medicine.name.lower() if char.isalpha()]

    def medicine_path(suffix: str) -> Callable[[Random], Request]:
        def draw(rng: Random) -> Request:
            code = quote(rng.choice(medicines).code, safe="")
            return Request(f"{API_PREFIX}/medicines/{code}{suffix}", {})

        return draw

    def search(
        term: Callable[[Random], str], *, may_be_empty: bool = False
    ) -> Callable[[Random], Request]:
        return lambda rng: Request(f"{API_PREFIX}/medicines", {"q": term(rng)}, may_be_empty)

    def name_part(rng: Random) -> str:
        name = rng.choice(medicines).name.lower()
        start = rng.randrange(max(len(name) - SELECTIVE_TERM_LENGTH, 0) + 1)
        return name[start : start + SELECTIVE_TERM_LENGTH]

    def misspelt(rng: Random) -> str:
        # A name read backwards: a term a person could type, which no medicine contains.
        return rng.choice(medicines).name.lower()[::-1]

    def dispenses(patient: bool, medicine: bool) -> Callable[[Random], Request]:
        def draw(rng: Random) -> Request:
            visit = rng.choice(visits)
            params = {"patient_ref": visit.patient_ref} if patient else {}
            if medicine:
                params["medicine"] = visit.medicine_code
            return Request(f"{API_PREFIX}/dispenses", params)

        return draw

    return [
        Scenario("GET /medicines", lambda rng: Request(f"{API_PREFIX}/medicines", {})),
        Scenario("GET /medicines?q=<one letter>", search(lambda rng: rng.choice(letters))),
        Scenario(
            f"GET /medicines?q=<{SELECTIVE_TERM_LENGTH} letters of a name>", search(name_part)
        ),
        Scenario("GET /medicines?q=<a code>", search(lambda rng: rng.choice(medicines).code)),
        Scenario("GET /medicines?q=<no match>", search(misspelt, may_be_empty=True)),
        Scenario("GET /dispenses", dispenses(patient=False, medicine=False)),
        Scenario("GET /dispenses?patient_ref=", dispenses(patient=True, medicine=False)),
        Scenario("GET /dispenses?medicine=", dispenses(patient=False, medicine=True)),
        Scenario("GET /dispenses?patient_ref=&medicine=", dispenses(patient=True, medicine=True)),
        Scenario("GET /medicines/{code}/rules", medicine_path("/rules")),
        Scenario("GET /medicines/{code}", medicine_path("")),
    ]


def _timed_get(client: httpx.Client, request: Request) -> float:
    started = time.perf_counter()
    response = client.get(request.path, params=request.params)
    elapsed_ms = (time.perf_counter() - started) * 1000
    _ensure_ok(response)
    # An empty page would be quick for the wrong reason: nearly every request asks for something
    # that exists.
    if not request.may_be_empty and response.json().get("items") == []:
        raise BenchError(f"GET {response.request.url} returned an empty page")
    return elapsed_ms


def _send(
    pool: ThreadPoolExecutor, clients: Sequence[httpx.Client], requests: list[Request]
) -> list[float]:
    """Send the requests, dealt out to the workers, and return every latency in milliseconds."""
    shares = [requests[worker :: len(clients)] for worker in range(len(clients))]
    sent = pool.map(lambda client, share: [_timed_get(client, r) for r in share], clients, shares)
    return [latency for share in sent for latency in share]


def run_benchmark(
    clients: Sequence[httpx.Client],
    scenarios: Sequence[Scenario],
    *,
    requests: int,
    warmup: int,
    rng: Random,
) -> list[Stats]:
    """Measure the scenarios one after the other, one worker per client. Each worker first sends
    `warmup` requests that are not counted, so that every connection is open and the server's
    caches are filled before anything is timed; `requests` are then shared among the workers."""
    results = []
    warm_requests = warmup * len(clients)
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        for scenario in scenarios:
            drawn = [scenario.draw(rng) for _ in range(warm_requests + requests)]
            _send(pool, clients, drawn[:warm_requests])
            results.append(summarise(scenario.label, _send(pool, clients, drawn[warm_requests:])))
    return results


def _size(count: int, complete: bool) -> str:
    return f"{count:,}" if complete else f"at least {count:,}"


def format_report(
    stats: Sequence[Stats],
    dataset: Dataset,
    *,
    base_url: str,
    requests: int,
    warmup: int,
    concurrency: int,
    budget_ms: float,
) -> str:
    medicines = _size(len(dataset.medicines), dataset.medicines_complete)
    dispenses = _size(len(dataset.visits), dataset.visits_complete)
    workers = f"{concurrency} worker{'s' if concurrency != 1 else ''}"
    method = (
        f"{requests} measured requests per endpoint after {warmup} warm-up requests per "
        f"worker, {workers} on keep-alive connections. Nearest-rank percentiles; budget: p95 below {budget_ms:g} ms."
    )
    lines = [
        f"## First-page latency of {base_url}",
        "",
        f"Dataset seen: {medicines} medicines, {dispenses} dispenses.",
        method,
        "",
        format_table(stats),
        "",
        format_verdict(stats, budget_ms),
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.bench",
        description="Measure the first-page latency of every read endpoint of a running server.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="(default: %(default)s)")
    for flag, default, what in (
        ("--requests", DEFAULT_REQUESTS, "measured requests per endpoint"),
        ("--warmup", DEFAULT_WARMUP, "requests per endpoint and worker sent first, not counted"),
        ("--concurrency", 1, "workers sending requests at the same time"),
    ):
        parser.add_argument(flag, type=int, default=default, help=f"{what} (default: %(default)s)")
    parser.add_argument(
        "--budget-ms",
        type=float,
        default=DEFAULT_BUDGET_MS,
        help="exit with status 1 if any p95 reaches this (default: %(default)s)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.requests < 1 or args.warmup < 0 or args.concurrency < 1 or args.budget_ms <= 0:
        parser.error(
            "--requests and --concurrency must be at least 1, --warmup 0 or more, "
            "and --budget-ms positive"
        )

    clients = [
        httpx.Client(base_url=args.base_url, timeout=TIMEOUT_SECONDS)
        for _ in range(args.concurrency)
    ]
    try:
        dataset = discover(clients[0])
        stats = run_benchmark(
            clients,
            build_scenarios(dataset),
            requests=args.requests,
            warmup=args.warmup,
            rng=Random(RANDOM_SEED),
        )
    except BenchError as error:
        print(error, file=sys.stderr)
        return 2
    except httpx.TransportError as error:
        print(f"Cannot get an answer from {args.base_url}: {error!r}", file=sys.stderr)
        return 2
    finally:
        for client in clients:
            client.close()
    print(
        format_report(
            stats,
            dataset,
            base_url=args.base_url,
            requests=args.requests,
            warmup=args.warmup,
            concurrency=args.concurrency,
            budget_ms=args.budget_ms,
        )
    )
    return 1 if over_budget(stats, args.budget_ms) else 0


if __name__ == "__main__":
    sys.exit(main())
