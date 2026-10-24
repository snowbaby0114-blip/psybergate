"""GET /api/v1/dispenses: newest first, filtered, and paged with a keyset cursor."""

import math
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.models import Dispense, FormularyRule, Medicine
from app.pagination import encode_cursor
from tests.api.dispense_support import DISPENSES, post_dispense
from tests.api.support import assert_problem, violation_pairs

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]
DispenseFactory = Callable[..., Dispense]

UNFILTERED = {"patient_ref": None, "medicine": None}


def at(day: int, hour: int = 10, micro: int = 0) -> datetime:
    """An instant in March 2026, in UTC."""
    return datetime(2026, 3, day, hour, 0, 0, micro, tzinfo=UTC)


def fetch(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get(DISPENSES, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


def walk(client: TestClient, start: str | None = None, **params: Any) -> list[list[int]]:
    """Follow next_cursor (from `start`, if given) until it is null; the ids of each page."""
    pages: list[list[int]] = []
    cursor = start
    while len(pages) < 1000:
        body = fetch(client, **params, **({"cursor": cursor} if cursor else {}))
        pages.append(ids(body))
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
    raise AssertionError("next_cursor never became null")


@pytest.fixture
def medicine(make_medicine: MedicineFactory) -> Medicine:
    return make_medicine(code="MED-0001", name="Amoxil")


@pytest.fixture
def rule(medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
    return make_rule(medicine, date(2026, 1, 1))


def test_an_empty_ledger_is_one_empty_page(client: TestClient) -> None:
    assert fetch(client) == {"items": [], "next_cursor": None}


def test_returns_the_documented_dispense_shape(
    client: TestClient, medicine: Medicine, rule: FormularyRule, make_dispense: DispenseFactory
) -> None:
    stored = make_dispense(
        medicine,
        rule,
        patient_ref="PT-7K2M9Q",
        quantity=20,
        dispensed_at=datetime(2026, 3, 15, 10, 0, tzinfo=UTC),
        authorisation_ref="AUTH-123",
        idempotency_key="7f6c",
    )

    body = fetch(client)

    [item] = body["items"]
    assert datetime.fromisoformat(item.pop("created_at")) == stored.created_at
    assert item == {
        "id": stored.id,
        "medicine_code": "MED-0001",
        "medicine_name": "Amoxil",
        "patient_ref": "PT-7K2M9Q",
        "quantity": 20,
        "dispensed_at": "2026-03-15T10:00:00Z",
        "authorisation_ref": "AUTH-123",
        "idempotency_key": "7f6c",
        "rule_id": rule.id,
    }
    assert body["next_cursor"] is None


def test_a_dispense_is_listed_exactly_as_it_was_returned_when_recorded(
    client: TestClient, rule: FormularyRule
) -> None:
    recorded = post_dispense(
        client, authorisation_ref="AUTH-1", dispensed_at="2026-03-15T10:00:00.5Z"
    )

    assert fetch(client)["items"] == [recorded.json()]


def test_a_rejection_is_not_a_dispense(client: TestClient, rule: FormularyRule) -> None:
    assert post_dispense(client, quantity=40).status_code == 422

    assert fetch(client) == {"items": [], "next_cursor": None}


class TestOrder:
    def test_newest_first_by_the_date_of_the_dispense_not_by_when_it_was_recorded(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        recorded_first_but_latest = make_dispense(medicine, rule, dispensed_at=at(20))
        recorded_second_but_oldest = make_dispense(medicine, rule, dispensed_at=at(1))
        recorded_last_but_middle = make_dispense(medicine, rule, dispensed_at=at(10))

        assert ids(fetch(client)) == [
            recorded_first_but_latest.id,
            recorded_last_but_middle.id,
            recorded_second_but_oldest.id,
        ]

    def test_dispenses_at_the_same_instant_come_the_highest_id_first(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        tied = [make_dispense(medicine, rule, dispensed_at=at(5)) for _ in range(4)]

        assert ids(fetch(client)) == [dispense.id for dispense in reversed(tied)]

    def test_instants_that_differ_by_a_microsecond_are_told_apart(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        made = [
            make_dispense(medicine, rule, dispensed_at=at(5, micro=micro))
            for micro in (3, 1, 5, 2, 4)
        ]
        expected = [
            dispense.id for dispense in sorted(made, key=lambda d: d.dispensed_at, reverse=True)
        ]

        pages = walk(client, limit=1)

        assert [page[0] for page in pages] == expected
        assert fetch(client)["items"][0]["dispensed_at"] == "2026-03-05T10:00:00.000005Z"


class TestPagination:
    @pytest.fixture
    def forty_one(
        self, medicine: Medicine, rule: FormularyRule, make_dispense: DispenseFactory
    ) -> list[int]:
        """41 dispenses on only 9 distinct instants, so most sort keys tie on the instant and
        the order depends on the id. Inserted in a scrambled order. Returns the ids in the order
        they must be listed."""
        made = [
            make_dispense(medicine, rule, dispensed_at=at(1 + (number * 4) % 9))
            for number in range(41)
        ]
        ordered = sorted(made, key=lambda d: (d.dispensed_at, d.id), reverse=True)
        return [dispense.id for dispense in ordered]

    @pytest.mark.parametrize("limit", [1, 2, 7, 10, 25, 40, 41, 42, 100])
    def test_walking_every_page_yields_each_dispense_once_in_order(
        self, client: TestClient, forty_one: list[int], limit: int
    ) -> None:
        pages = walk(client, limit=limit)

        assert [dispense_id for page in pages for dispense_id in page] == forty_one
        assert len(pages) == math.ceil(41 / limit)
        assert all(len(page) == limit for page in pages[:-1])

    def test_the_default_page_size_is_25(self, client: TestClient, forty_one: list[int]) -> None:
        body = fetch(client)

        assert ids(body) == forty_one[:25]
        assert body["next_cursor"] is not None

    def test_the_last_page_of_an_exact_multiple_has_no_cursor(
        self, client: TestClient, forty_one: list[int]
    ) -> None:
        body = fetch(client, limit=41)

        assert ids(body) == forty_one
        assert body["next_cursor"] is None

    def test_limit_100_returns_100_when_more_exist(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        for _ in range(101):
            make_dispense(medicine, rule)

        body = fetch(client, limit=100)

        assert len(body["items"]) == 100
        assert len(fetch(client, limit=100, cursor=body["next_cursor"])["items"]) == 1

    def test_a_backdated_dispense_recorded_mid_walk_turns_up_later_and_nothing_repeats(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        made = [make_dispense(medicine, rule, dispensed_at=at(day)) for day in range(1, 31)]
        first = fetch(client, limit=10)

        backdated = make_dispense(medicine, rule, dispensed_at=at(15, hour=12))
        newest = make_dispense(medicine, rule, dispensed_at=at(31))
        rest = [
            dispense_id
            for page in walk(client, first["next_cursor"], limit=10)
            for dispense_id in page
        ]

        seen = ids(first) + rest
        assert len(seen) == len(set(seen))
        assert newest.id not in seen
        older = [dispense.id for dispense in reversed(made[:20])]
        assert rest == [*older[:5], backdated.id, *older[5:]]
        assert seen[:10] == [dispense.id for dispense in reversed(made[20:])]

    def test_a_dispense_recorded_mid_walk_with_the_instant_of_the_cursor_is_not_repeated(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        made = [make_dispense(medicine, rule, dispensed_at=at(5)) for _ in range(6)]
        first = fetch(client, limit=3)
        late_tie = make_dispense(medicine, rule, dispensed_at=at(5))

        rest = [
            dispense_id
            for page in walk(client, first["next_cursor"], limit=3)
            for dispense_id in page
        ]

        assert ids(first) + rest == [dispense.id for dispense in reversed(made)]
        assert late_tie.id not in ids(first) + rest

    def test_a_cursor_is_valid_for_the_same_filters_however_they_are_spelt_around(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        for number in range(3):
            make_dispense(medicine, rule, dispensed_at=at(number + 1))
        cursor = fetch(client, patient_ref="PT-1", limit=1)["next_cursor"]

        assert len(fetch(client, patient_ref=" PT-1 ", limit=1, cursor=cursor)["items"]) == 1


class TestFilters:
    @pytest.fixture
    def ledger(
        self,
        medicine: Medicine,
        rule: FormularyRule,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
        make_dispense: DispenseFactory,
    ) -> dict[str, int]:
        """Four dispenses: (PT-1, MED-0001) oldest, (PT-1, MED-0002), (PT-2, MED-0001) and
        (PT-2, MED-0002) newest. Returns their ids by those names."""
        other = make_medicine(code="MED-0002", name="Panado", is_active=False)
        other_rule = make_rule(other, date(2026, 1, 1))
        return {
            "p1m1": make_dispense(medicine, rule, patient_ref="PT-1", dispensed_at=at(1)).id,
            "p1m2": make_dispense(other, other_rule, patient_ref="PT-1", dispensed_at=at(2)).id,
            "p2m1": make_dispense(medicine, rule, patient_ref="PT-2", dispensed_at=at(3)).id,
            "p2m2": make_dispense(other, other_rule, patient_ref="PT-2", dispensed_at=at(4)).id,
        }

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({}, ["p2m2", "p2m1", "p1m2", "p1m1"]),
            ({"patient_ref": "PT-1"}, ["p1m2", "p1m1"]),
            ({"patient_ref": "PT-2"}, ["p2m2", "p2m1"]),
            ({"medicine": "MED-0001"}, ["p2m1", "p1m1"]),
            ({"medicine": "MED-0002"}, ["p2m2", "p1m2"]),
            ({"patient_ref": "PT-1", "medicine": "MED-0002"}, ["p1m2"]),
            ({"patient_ref": "PT-2", "medicine": "MED-0001"}, ["p2m1"]),
            ({"patient_ref": "  PT-2 ", "medicine": " MED-0001  "}, ["p2m1"]),
        ],
    )
    def test_filters_by_patient_and_by_medicine_and_by_both(
        self,
        client: TestClient,
        ledger: dict[str, int],
        params: dict[str, str],
        expected: list[str],
    ) -> None:
        assert ids(fetch(client, **params)) == [ledger[name] for name in expected]

    @pytest.mark.parametrize(
        "params",
        [
            {"patient_ref": "PT-3"},
            {"patient_ref": "pt-1"},
            {"patient_ref": "PT"},
            {"patient_ref": "PT-%"},
            {"medicine": "med-0001"},
            {"medicine": "MED"},
            {"medicine": "MED-%"},
            {"medicine": "NOPE"},
            {"patient_ref": "PT-1", "medicine": "NOPE"},
        ],
    )
    def test_the_filters_match_exactly_and_an_unknown_value_is_an_empty_page(
        self, client: TestClient, ledger: dict[str, int], params: dict[str, str]
    ) -> None:
        assert fetch(client, **params) == {"items": [], "next_cursor": None}

    def test_a_medicine_code_is_matched_as_given_even_with_spaces_and_symbols(
        self,
        client: TestClient,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
        make_dispense: DispenseFactory,
    ) -> None:
        odd = make_medicine(code="A B+C%D&E=F")
        made = make_dispense(odd, make_rule(odd, date(2026, 1, 1)), dispensed_at=at(9))

        assert ids(fetch(client, medicine="A B+C%D&E=F")) == [made.id]

    def test_a_filtered_listing_pages_like_any_other(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        mine = [
            make_dispense(medicine, rule, patient_ref="PT-1", dispensed_at=at(day))
            for day in range(1, 8)
        ]
        for day in range(1, 8):
            make_dispense(medicine, rule, patient_ref="PT-2", dispensed_at=at(day, hour=11))

        pages = walk(client, patient_ref="PT-1", limit=3)

        assert pages == [
            [d.id for d in reversed(mine)][:3],
            [d.id for d in reversed(mine)][3:6],
            [d.id for d in reversed(mine)][6:],
        ]

    def test_the_dispenses_of_an_inactive_medicine_are_listed_too(
        self, client: TestClient, ledger: dict[str, int]
    ) -> None:
        listed = fetch(client, medicine="MED-0002")["items"]

        assert [item["medicine_code"] for item in listed] == ["MED-0002", "MED-0002"]

    @pytest.mark.parametrize(
        ("params", "code"),
        [
            ({"patient_ref": ""}, "too_short"),
            ({"patient_ref": "   "}, "too_short"),
            ({"patient_ref": "x" * 129}, "too_long"),
            ({"patient_ref": "a\x00b"}, "invalid_format"),
            ({"medicine": ""}, "too_short"),
            ({"medicine": "x" * 65}, "too_long"),
            ({"medicine": "a\x1bb"}, "invalid_format"),
        ],
    )
    def test_a_filter_must_be_a_reasonable_value(
        self, client: TestClient, params: dict[str, str], code: str
    ) -> None:
        response = client.get(DISPENSES, params=params)

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [(code, next(iter(params)))]


class TestLimit:
    @pytest.mark.parametrize(
        ("limit", "code"),
        [
            ("0", "out_of_range"),
            ("-1", "out_of_range"),
            ("101", "out_of_range"),
            ("abc", "invalid_format"),
            ("", "invalid_format"),
            ("2.5", "invalid_format"),
        ],
    )
    def test_must_be_a_whole_number_from_1_to_100(
        self, client: TestClient, limit: str, code: str
    ) -> None:
        response = client.get(DISPENSES, params={"limit": limit})

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [(code, "limit")]

    @pytest.mark.parametrize("limit", [1, 100])
    def test_the_bounds_are_accepted(self, client: TestClient, limit: int) -> None:
        assert fetch(client, limit=limit) == {"items": [], "next_cursor": None}


class TestCursor:
    @pytest.fixture
    def three(
        self, medicine: Medicine, rule: FormularyRule, make_dispense: DispenseFactory
    ) -> list[Dispense]:
        return [make_dispense(medicine, rule, dispensed_at=at(day)) for day in (1, 2, 3)]

    @pytest.mark.parametrize(
        ("issued_for", "used_with"),
        [
            pytest.param({}, {"patient_ref": "PT-1"}, id="unfiltered, then a patient"),
            pytest.param({}, {"medicine": "MED-0001"}, id="unfiltered, then a medicine"),
            pytest.param({"patient_ref": "PT-1"}, {}, id="a patient, then unfiltered"),
            pytest.param({"patient_ref": "PT-1"}, {"patient_ref": "PT-2"}, id="another patient"),
            pytest.param({"medicine": "MED-0001"}, {"medicine": "MED-0002"}, id="another medicine"),
            pytest.param(
                {"patient_ref": "PT-1"},
                {"patient_ref": "PT-1", "medicine": "MED-0001"},
                id="a medicine added",
            ),
            pytest.param({"medicine": "MED-0001"}, {"medicine": "NOPE"}, id="an unknown medicine"),
        ],
    )
    def test_a_cursor_is_refused_with_filters_other_than_it_was_issued_for(
        self,
        client: TestClient,
        three: list[Dispense],
        issued_for: dict[str, str],
        used_with: dict[str, str],
    ) -> None:
        cursor = fetch(client, limit=1, **issued_for)["next_cursor"]

        response = client.get(DISPENSES, params={**used_with, "cursor": cursor})

        body = assert_problem(response, 400, "invalid_cursor")
        assert violation_pairs(body) == [("invalid_cursor", "cursor")]

    @pytest.mark.parametrize(
        "cursor",
        [
            "not-a-cursor",
            "",
            "e30",  # base64url of {}
            encode_cursor([], UNFILTERED),
            encode_cursor(["2026-03-02T10:00:00.000000+00:00"], UNFILTERED),
            encode_cursor(["2026-03-02T10:00:00.000000+00:00", 1, 2], UNFILTERED),
            encode_cursor([1, 2], UNFILTERED),
            encode_cursor([None, 2], UNFILTERED),
            encode_cursor(["2026-03-02T10:00:00.000000+00:00", "2"], UNFILTERED),
            encode_cursor(["2026-03-02T10:00:00.000000+00:00", 2.5], UNFILTERED),
            encode_cursor(["2026-03-02T10:00:00.000000+00:00", True], UNFILTERED),
            encode_cursor(["2026-03-02T10:00:00.000000+00:00", None], UNFILTERED),
            encode_cursor(["2026-03-02T10:00:00.000000+00:00", -1], UNFILTERED),
            encode_cursor(["2026-03-02T10:00:00.000000+00:00", 2**63], UNFILTERED),
            encode_cursor(["2026-03-02T10:00:00.000000", 2], UNFILTERED),  # no offset
            encode_cursor(["2026-03-02T12:00:00.000000+02:00", 2], UNFILTERED),  # not UTC
            encode_cursor(["0001-01-01T00:00:00+05:00", 2], UNFILTERED),  # would overflow in UTC
            encode_cursor(["9999-12-31T23:59:59-05:00", 2], UNFILTERED),
            encode_cursor(["2026-03-02", 2], UNFILTERED),
            encode_cursor(["yesterday", 2], UNFILTERED),
            encode_cursor(
                ["2026-03-02T10:00:00+00:00\x00", 2], UNFILTERED
            ),  # PostgreSQL cannot store NUL
            encode_cursor(["2026-03-02T10:00:00+00:00\ud800", 2], UNFILTERED),
            encode_cursor([[], {}], UNFILTERED),
        ],
    )
    def test_a_cursor_that_is_malformed_or_forged_is_refused_and_never_a_500(
        self, client: TestClient, three: list[Dispense], cursor: str
    ) -> None:
        response = client.get(DISPENSES, params={"cursor": cursor})

        body = assert_problem(response, 400, "invalid_cursor")
        assert violation_pairs(body) == [("invalid_cursor", "cursor")]

    def test_a_tampered_cursor_is_refused(self, client: TestClient, three: list[Dispense]) -> None:
        cursor = fetch(client, limit=1)["next_cursor"]
        tampered = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")

        assert_problem(client.get(DISPENSES, params={"cursor": tampered}), 400, "invalid_cursor")

    def test_a_cursor_of_the_shape_the_listing_issues_resumes_after_that_position(
        self, client: TestClient, three: list[Dispense]
    ) -> None:
        cursor = encode_cursor([at(3).isoformat(timespec="microseconds"), three[2].id], UNFILTERED)

        assert ids(fetch(client, cursor=cursor)) == [three[1].id, three[0].id]

    def test_the_position_is_the_last_item_of_the_page(
        self, client: TestClient, three: list[Dispense]
    ) -> None:
        first = fetch(client, limit=2)

        assert ids(first) == [three[2].id, three[1].id]
        assert ids(fetch(client, limit=2, cursor=first["next_cursor"])) == [three[0].id]
