"""The plan PostgreSQL uses for every query the repositories run, on a dataset big enough for plans
to matter: 153,000 medicines, 30,000 dispenses by 12,000 patients (the seed command plus a bulk
catalogue, so that the trigram indexes are worth using; see tests/perf/dataset.py).

Each query is run by its repository function, the statement that reaches the driver is recorded,
and PostgreSQL EXPLAINs it with its parameters bound. A test names the index the query is meant to
use (the names of SPEC 4) and fails if a table is read from end to end.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine

from app import repositories
from app.domain.timeutil import history_bounds_utc, to_local_date
from tests.perf.dataset import BULK_MEDICINES, Samples, large_dataset
from tests.perf.plan_support import Plan, Query, explain, run_repeatedly, select_run_by

# Building the dataset takes about 20 s here; the default timeout of 120 s is for ordinary tests.
pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]

LIMIT = 25
# A page statement asks for one row more than the page, to learn whether there is a next page.
PAGE_ROWS = LIMIT + 1
# psycopg prepares a statement on its sixth execution on a connection, and PostgreSQL decides
# between custom and generic plans from the sixth execution of a prepared statement on: after
# twenty runs it has had every chance to switch.
RUNS = 20
UNPREPARED_RUNS = 5

NAME_INDEX = "ix_medicines_name_code"
TIME_INDEX = "ix_dispenses_time"
MEDICINE_INDEX = "ix_dispenses_medicine_time"
PATIENT_INDEX = "ix_dispenses_patient_medicine_time"
RULES_INDEX = "ix_formulary_rules_medicine_id_effective_from"
MEDICINE_PK = "pk_medicines"
TRIGRAM_INDEXES = {"ix_medicines_name_trgm", "ix_medicines_code_trgm"}


@pytest.fixture(scope="module")
def data(engine: Engine) -> Iterator[Samples]:
    with large_dataset(engine) as samples:
        assert samples.medicines > BULK_MEDICINES
        assert samples.dispenses >= 30_000
        assert samples.patients >= 10_000
        yield samples


def plan_of(engine: Engine, query: Query) -> Plan:
    return explain(engine, select_run_by(engine, query))


def assert_ordered_scan_stops(plan: Plan, index: str, *, full_page: bool = True) -> None:
    """The index delivers the rows in the order of the page and the scan ends with the page: no
    sort, and no row read only to be thrown away."""
    node = plan.index_node(index)
    if full_page:
        assert node["Actual Rows"] == PAGE_ROWS
    else:
        assert node["Actual Rows"] <= PAGE_ROWS
    assert "Rows Removed by Filter" not in node
    assert plan.sorts == 0


def assert_in_index_condition(plan: Plan, index: str, condition: str) -> None:
    """The condition is an index condition (a seek), not a filter applied to rows already read."""
    node = plan.index_node(index)
    assert condition in node["Index Cond"]
    assert "Filter" not in node


def search(data: Samples, term: str | None, *, cursor: bool = False) -> Query:
    after = data.medicine_cursor if cursor else None
    return lambda s: repositories.search_medicines(s, term, after, LIMIT)


class TestMedicineSearch:
    def test_the_first_page_reads_the_name_index_in_order(
        self, engine: Engine, data: Samples
    ) -> None:
        plan = plan_of(engine, search(data, None))

        assert plan.indexes == {NAME_INDEX}
        assert plan.sequentially_scanned == set()
        assert_ordered_scan_stops(plan, NAME_INDEX)

    def test_a_cursor_is_a_seek_into_the_name_index(self, engine: Engine, data: Samples) -> None:
        plan = plan_of(engine, search(data, None, cursor=True))

        assert plan.indexes == {NAME_INDEX}
        assert_ordered_scan_stops(plan, NAME_INDEX)
        assert_in_index_condition(plan, NAME_INDEX, "ROW(name, code) >")

    @pytest.mark.parametrize("with_cursor", [False, True], ids=["first page", "cursor"])
    @pytest.mark.parametrize("term", ["rare_name_part", "rare_code"])
    def test_a_selective_term_is_found_through_both_trigram_indexes(
        self, engine: Engine, data: Samples, term: str, with_cursor: bool
    ) -> None:
        plan = plan_of(engine, search(data, getattr(data, term), cursor=with_cursor))

        assert plan.indexes == TRIGRAM_INDEXES
        assert plan.sequentially_scanned == set()
        # A sequential scan of the catalogue reads about 1,600 pages.
        assert plan.buffers < 1000

    @pytest.mark.parametrize("with_cursor", [False, True], ids=["first page", "cursor"])
    def test_a_term_that_most_names_contain_walks_the_name_index_only_as_far_as_the_page(
        self, engine: Engine, data: Samples, with_cursor: bool
    ) -> None:
        plan = plan_of(engine, search(data, "a", cursor=with_cursor))

        assert plan.indexes == {NAME_INDEX}
        assert plan.sequentially_scanned == set()
        assert plan.sorts == 0
        node = plan.index_node(NAME_INDEX)
        assert node["Actual Rows"] == PAGE_ROWS
        # Matches are plentiful, so the filter throws away only a few rows on the way.
        assert node["Rows Removed by Filter"] < PAGE_ROWS * 2


def listing(data: Samples, *, patient: bool, medicine: bool, cursor: bool = False) -> Query:
    """The dispense listing for the busiest patient and medicine; for both filters, for the pair
    that was dispensed together most often."""
    if patient and medicine:
        patient_ref, medicine_id = data.pair_patient_ref, data.pair_medicine_id
    else:
        patient_ref, medicine_id = data.patient_ref, data.medicine_id
    after = data.dispense_cursor if cursor else None
    return lambda s: repositories.list_dispenses(
        s, patient_ref if patient else None, medicine_id if medicine else None, after, LIMIT
    )


class TestDispenseListing:
    @pytest.mark.parametrize("with_cursor", [False, True], ids=["first page", "cursor"])
    @pytest.mark.parametrize(
        ("patient", "medicine", "indexes"),
        [
            pytest.param(False, False, {TIME_INDEX, MEDICINE_PK}, id="unfiltered"),
            pytest.param(True, False, {PATIENT_INDEX, MEDICINE_PK}, id="patient"),
            pytest.param(False, True, {MEDICINE_INDEX, MEDICINE_PK}, id="medicine"),
            pytest.param(True, True, {PATIENT_INDEX, MEDICINE_PK}, id="both"),
        ],
    )
    def test_it_uses_the_intended_indexes_and_reads_no_table_through(
        self,
        engine: Engine,
        data: Samples,
        patient: bool,
        medicine: bool,
        indexes: set[str],
        with_cursor: bool,
    ) -> None:
        plan = plan_of(
            engine, listing(data, patient=patient, medicine=medicine, cursor=with_cursor)
        )

        assert plan.indexes == indexes
        assert plan.sequentially_scanned == set()

    @pytest.mark.parametrize("with_cursor", [False, True], ids=["first page", "cursor"])
    @pytest.mark.parametrize(
        ("patient", "medicine", "index", "full_page"),
        [
            pytest.param(False, False, TIME_INDEX, True, id="unfiltered"),
            pytest.param(False, True, MEDICINE_INDEX, True, id="medicine"),
            # One patient has less than a page of one medicine.
            pytest.param(True, True, PATIENT_INDEX, False, id="both"),
        ],
    )
    def test_the_index_delivers_the_newest_first_order_and_the_scan_stops_after_the_page(
        self,
        engine: Engine,
        data: Samples,
        patient: bool,
        medicine: bool,
        index: str,
        full_page: bool,
        with_cursor: bool,
    ) -> None:
        plan = plan_of(
            engine, listing(data, patient=patient, medicine=medicine, cursor=with_cursor)
        )

        assert_ordered_scan_stops(plan, index, full_page=full_page)

    @pytest.mark.parametrize(
        ("patient", "medicine", "index"),
        [
            pytest.param(False, False, TIME_INDEX, id="unfiltered"),
            pytest.param(True, False, PATIENT_INDEX, id="patient"),
            pytest.param(False, True, MEDICINE_INDEX, id="medicine"),
            pytest.param(True, True, PATIENT_INDEX, id="both"),
        ],
    )
    def test_the_cursor_is_a_seek_into_the_index_not_a_filter(
        self, engine: Engine, data: Samples, patient: bool, medicine: bool, index: str
    ) -> None:
        plan = plan_of(engine, listing(data, patient=patient, medicine=medicine, cursor=True))

        assert_in_index_condition(plan, index, "ROW(dispensed_at, id) <")


def history(data: Samples) -> Query:
    start, end = history_bounds_utc(to_local_date(data.pair_last_dispensed_at))
    return lambda s: repositories.pair_history(
        s,
        medicine_id=data.pair_medicine_id,
        patient_ref=data.pair_patient_ref,
        start=start,
        end=end,
    )


def summary(data: Samples, *, end: datetime | None = None) -> Query:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return lambda s: repositories.dispense_summary(s, data.medicine_id, start=start, end=end)


class TestOtherQueries:
    def test_the_history_of_one_patient_and_medicine_is_a_range_of_the_pair_index(
        self, engine: Engine, data: Samples
    ) -> None:
        plan = plan_of(engine, history(data))

        assert plan.indexes == {PATIENT_INDEX}
        assert plan.sequentially_scanned == set()
        assert plan.sorts == 0
        node = plan.index_node(PATIENT_INDEX)
        for bound in ("patient_ref =", "medicine_id =", "dispensed_at >=", "dispensed_at <"):
            assert bound in node["Index Cond"]
        assert "Filter" not in node

    def test_the_rules_of_a_medicine_come_in_order_from_their_index(
        self, engine: Engine, data: Samples
    ) -> None:
        plan = plan_of(engine, lambda s: repositories.list_rules(s, data.medicine_id))

        assert plan.indexes == {RULES_INDEX}
        assert plan.sequentially_scanned == set()
        assert plan.sorts == 0

    @pytest.mark.parametrize("bounded", [False, True], ids=["open", "bounded"])
    def test_the_recorded_dispenses_of_a_medicine_are_counted_from_its_index(
        self, engine: Engine, data: Samples, bounded: bool
    ) -> None:
        end = datetime(2026, 1, 1, tzinfo=UTC) if bounded else None

        plan = plan_of(engine, summary(data, end=end))

        assert plan.indexes == {MEDICINE_INDEX}
        assert plan.sequentially_scanned == set()

    def test_a_medicine_is_found_by_its_code_through_the_unique_index(
        self, engine: Engine, data: Samples
    ) -> None:
        plan = plan_of(engine, lambda s: repositories.get_medicine_by_code(s, data.medicine_code))

        assert plan.indexes == {"uq_medicines_code"}
        assert plan.sequentially_scanned == set()

    def test_an_idempotency_key_is_looked_up_through_the_unique_index(
        self, engine: Engine, data: Samples
    ) -> None:
        plan = plan_of(engine, lambda s: repositories.find_attempt_by_key(s, data.attempt_key))

        assert plan.indexes == {"uq_dispense_attempts_idempotency_key"}
        assert plan.sequentially_scanned == set()


# Every statement, with the indexes its generic plan may use. The one place where PostgreSQL's
# generic plan differs from the plan above is the history: it reads the medicine's index and
# filters by patient. That is bounded by one medicine's dispenses in the 59 days of the window,
# and was measured to cost less than the round trip that would avoid it (see
# repositories.pair_history). Either index is acceptable there.
GENERIC_PLAN_CASES: dict[str, Callable[[Samples], tuple[Query, set[str]]]] = {
    "medicine page": lambda d: (search(d, None), {NAME_INDEX}),
    "medicine cursor": lambda d: (search(d, None, cursor=True), {NAME_INDEX}),
    "dispenses": lambda d: (
        listing(d, patient=False, medicine=False),
        {TIME_INDEX, MEDICINE_PK},
    ),
    "dispenses cursor": lambda d: (
        listing(d, patient=False, medicine=False, cursor=True),
        {TIME_INDEX, MEDICINE_PK},
    ),
    "patient": lambda d: (listing(d, patient=True, medicine=False), {PATIENT_INDEX, MEDICINE_PK}),
    "patient cursor": lambda d: (
        listing(d, patient=True, medicine=False, cursor=True),
        {PATIENT_INDEX, MEDICINE_PK},
    ),
    "medicine": lambda d: (
        listing(d, patient=False, medicine=True),
        {MEDICINE_INDEX, MEDICINE_PK},
    ),
    "medicine filter cursor": lambda d: (
        listing(d, patient=False, medicine=True, cursor=True),
        {MEDICINE_INDEX, MEDICINE_PK},
    ),
    "both": lambda d: (listing(d, patient=True, medicine=True), {PATIENT_INDEX, MEDICINE_PK}),
    "both cursor": lambda d: (
        listing(d, patient=True, medicine=True, cursor=True),
        {PATIENT_INDEX, MEDICINE_PK},
    ),
    "history": lambda d: (history(d), {PATIENT_INDEX, MEDICINE_INDEX}),
    "rules": lambda d: (lambda s: repositories.list_rules(s, d.medicine_id), {RULES_INDEX}),
    "summary": lambda d: (summary(d), {MEDICINE_INDEX}),
    "medicine by code": lambda d: (
        lambda s: repositories.get_medicine_by_code(s, d.medicine_code),
        {"uq_medicines_code"},
    ),
    "attempt by key": lambda d: (
        lambda s: repositories.find_attempt_by_key(s, d.attempt_key),
        {"uq_dispense_attempts_idempotency_key"},
    ),
}


class TestAfterThePlanIsGeneric:
    """PostgreSQL plans a prepared statement for the values of its first five executions and may
    then adopt one generic plan for all later ones, chosen without knowing the values."""

    @pytest.mark.parametrize("name", GENERIC_PLAN_CASES)
    def test_the_generic_plan_of_every_query_still_uses_an_index_and_reads_no_table_through(
        self, engine: Engine, data: Samples, name: str
    ) -> None:
        query, acceptable = GENERIC_PLAN_CASES[name](data)

        prepared, generic = run_repeatedly(engine, query, RUNS)

        # The statement really was prepared, so these runs are the ones that can go generic.
        assert prepared.custom_plans + prepared.generic_plans == RUNS - UNPREPARED_RUNS
        assert generic.indexes
        assert generic.indexes <= acceptable
        assert generic.sequentially_scanned == set()


class TestTheSearchTermNeverGetsAGenericPlan:
    """The estimate for `ILIKE '%term%'` depends entirely on the term. Planned without it, the
    statement walks the name index and filters, which is right for a term most names contain and
    far slower for a rare one. The search therefore tells PostgreSQL to plan for the term every
    time (repositories.search_medicines)."""

    def test_twenty_searches_on_one_connection_all_get_a_plan_made_for_their_term(
        self, engine: Engine, data: Samples
    ) -> None:
        prepared, _ = run_repeatedly(engine, search(data, data.rare_name_part), RUNS)

        assert prepared.custom_plans == RUNS - UNPREPARED_RUNS
        assert prepared.generic_plans == 0

    def test_the_generic_plan_would_not_use_the_trigram_indexes(
        self, engine: Engine, data: Samples
    ) -> None:
        _, generic = run_repeatedly(engine, search(data, data.rare_name_part), RUNS)

        assert generic.indexes == {NAME_INDEX}
