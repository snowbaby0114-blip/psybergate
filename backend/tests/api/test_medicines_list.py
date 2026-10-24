"""GET /api/v1/medicines: search, ordering and keyset pagination."""

import math
from collections.abc import Callable
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.models import Medicine
from app.pagination import encode_cursor
from tests.api.support import assert_problem, violation_pairs

MEDICINES = "/api/v1/medicines"
NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf"]


def fetch(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get(MEDICINES, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def walk(client: TestClient, start: str | None = None, **params: Any) -> list[list[str]]:
    """Follow next_cursor (from `start`, if given) until it is null; the codes of each page."""
    pages: list[list[str]] = []
    cursor = start
    while len(pages) < 1000:
        body = fetch(client, **params, **({"cursor": cursor} if cursor else {}))
        pages.append([item["code"] for item in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
    raise AssertionError("next_cursor never became null")


def codes(body: dict[str, Any]) -> list[str]:
    return [item["code"] for item in body["items"]]


def test_returns_the_documented_medicine_shape(
    client: TestClient, make_medicine: Callable[..., Medicine]
) -> None:
    make_medicine(code="MED-0001", name="Amoxil", form="capsule", strength_value=500)

    response = client.get(MEDICINES)

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "code": "MED-0001",
                "name": "Amoxil",
                "form": "capsule",
                "strength_value": 500,
                "strength_unit": "mg",
                "is_active": True,
            }
        ],
        "next_cursor": None,
    }


def test_strength_is_a_json_number_even_when_fractional(
    client: TestClient, make_medicine: Callable[..., Medicine]
) -> None:
    make_medicine(strength_value="0.125")

    response = client.get(MEDICINES)

    assert '"strength_value":0.125' in response.text


def test_an_empty_catalogue_is_one_empty_page(client: TestClient) -> None:
    assert fetch(client) == {"items": [], "next_cursor": None}


def test_orders_by_name_then_code(
    client: TestClient, make_medicine: Callable[..., Medicine]
) -> None:
    make_medicine(code="MED-0003", name="Bravo")
    make_medicine(code="MED-0002", name="Alpha")
    make_medicine(code="MED-0001", name="Alpha")

    assert codes(fetch(client)) == ["MED-0001", "MED-0002", "MED-0003"]


def test_inactive_medicines_are_listed_too(
    client: TestClient, make_medicine: Callable[..., Medicine]
) -> None:
    make_medicine(code="MED-0001", is_active=False)

    assert fetch(client)["items"][0]["is_active"] is False


class TestPagination:
    @pytest.fixture
    def sixty_five(self, make_medicine: Callable[..., Medicine]) -> list[str]:
        """65 medicines with only seven distinct names, so most sort keys tie on name and the
        order depends on the code tiebreak. Inserted in a scrambled order."""
        made = [
            make_medicine(code=f"MED-{(i * 37) % 65 + 1:04d}", name=NAMES[(i * 11) % 7])
            for i in range(65)
        ]
        ordered = sorted(made, key=lambda medicine: (medicine.name, medicine.code))
        return [medicine.code for medicine in ordered]

    @pytest.mark.parametrize("limit", [1, 7, 10, 25, 64, 65, 66, 100])
    def test_walking_every_page_yields_each_medicine_once_in_order(
        self, client: TestClient, sixty_five: list[str], limit: int
    ) -> None:
        pages = walk(client, limit=limit)

        assert [code for page in pages for code in page] == sixty_five
        assert len(pages) == math.ceil(65 / limit)
        assert all(len(page) == limit for page in pages[:-1])

    def test_default_page_size_is_25(self, client: TestClient, sixty_five: list[str]) -> None:
        body = fetch(client)

        assert codes(body) == sixty_five[:25]
        assert body["next_cursor"] is not None

    def test_the_last_page_of_an_exact_multiple_has_no_cursor(
        self, client: TestClient, make_medicine: Callable[..., Medicine]
    ) -> None:
        for _ in range(20):
            make_medicine()

        pages = walk(client, limit=10)

        assert [len(page) for page in pages] == [10, 10]

    def test_limit_100_returns_100_when_more_exist(
        self, client: TestClient, make_medicine: Callable[..., Medicine]
    ) -> None:
        for _ in range(101):
            make_medicine()

        body = fetch(client, limit=100)

        assert len(body["items"]) == 100
        assert len(fetch(client, limit=100, cursor=body["next_cursor"])["items"]) == 1

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
    def test_rejects_a_limit_outside_1_to_100(
        self, client: TestClient, limit: str, code: str
    ) -> None:
        response = client.get(MEDICINES, params={"limit": limit})

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [(code, "limit")]

    def test_a_medicine_added_mid_walk_never_duplicates_or_hides_others(
        self,
        client: TestClient,
        make_medicine: Callable[..., Medicine],
        sixty_five: list[str],
    ) -> None:
        first = fetch(client, limit=10)
        before = make_medicine(code="MED-9998", name="Aaa")
        after = make_medicine(code="MED-9999", name="Zulu")

        rest = [code for page in walk(client, first["next_cursor"], limit=10) for code in page]

        seen = codes(first) + rest
        assert len(seen) == len(set(seen))
        assert seen == sixty_five + [after.code]
        assert before.code not in seen

    @pytest.mark.parametrize(
        "cursor",
        [
            "not-a-cursor",
            "",
            "e30",  # base64url of {}
            encode_cursor(["a"], {"q": None}),  # wrong arity
            encode_cursor(["a", "b", "c"], {"q": None}),
            encode_cursor([1, 2], {"q": None}),  # not text
            encode_cursor(["a", None], {"q": None}),
            encode_cursor(["a\x00", "b"], {"q": None}),  # PostgreSQL cannot store NUL
            encode_cursor(["a", "\ud800"], {"q": None}),  # not encodable as UTF-8
            encode_cursor(["a", "b"], {"q": "other"}),  # issued for another search term
        ],
    )
    def test_rejects_a_cursor_that_is_malformed_forged_or_for_another_query(
        self, client: TestClient, cursor: str
    ) -> None:
        response = client.get(MEDICINES, params={"cursor": cursor})

        body = assert_problem(response, 400, "invalid_cursor")
        assert violation_pairs(body) == [("invalid_cursor", "cursor")]

    def test_rejects_a_tampered_cursor(
        self, client: TestClient, make_medicine: Callable[..., Medicine]
    ) -> None:
        for _ in range(3):
            make_medicine()
        cursor = fetch(client, limit=1)["next_cursor"]
        tampered = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")

        assert_problem(client.get(MEDICINES, params={"cursor": tampered}), 400, "invalid_cursor")

    def test_a_cursor_from_an_unfiltered_listing_is_refused_with_a_search_term(
        self, client: TestClient, make_medicine: Callable[..., Medicine]
    ) -> None:
        for _ in range(3):
            make_medicine()
        cursor = fetch(client, limit=1)["next_cursor"]

        response = client.get(MEDICINES, params={"q": "amox", "cursor": cursor})

        assert_problem(response, 400, "invalid_cursor")


class TestSearch:
    @pytest.fixture
    def catalogue(self, make_medicine: Callable[..., Medicine]) -> None:
        make_medicine(code="MED-0001", name="Amoxicillin")
        make_medicine(code="MED-0002", name="Amoxil")
        make_medicine(code="MED-0003", name="Paracetamol")
        make_medicine(code="MED-0004", name="50% Saline")
        make_medicine(code="MED-0005", name="Vitamin_C")
        make_medicine(code="MED-0006", name="Vitamin C")
        make_medicine(code="MED-0007", name=r"Back\slash")
        make_medicine(code="MED-0008", name="IBUPROFEN")

    @pytest.mark.usefixtures("catalogue")
    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("amox", ["MED-0001", "MED-0002"]),
            ("AMOX", ["MED-0001", "MED-0002"]),
            ("moxi", ["MED-0001", "MED-0002"]),
            ("illin", ["MED-0001"]),
            ("ibup", ["MED-0008"]),
            ("IBUP", ["MED-0008"]),
        ],
    )
    def test_matches_part_of_the_name_ignoring_case(
        self, client: TestClient, q: str, expected: list[str]
    ) -> None:
        assert codes(fetch(client, q=q)) == expected

    @pytest.mark.usefixtures("catalogue")
    def test_matches_part_of_the_code(self, client: TestClient) -> None:
        assert codes(fetch(client, q="med-0003")) == ["MED-0003"]
        assert codes(fetch(client, q="0007")) == ["MED-0007"]

    @pytest.mark.usefixtures("catalogue")
    def test_matches_name_or_code_in_one_search(
        self, client: TestClient, make_medicine: Callable[..., Medicine]
    ) -> None:
        make_medicine(code="PARA-1", name="Something else")

        assert codes(fetch(client, q="para")) == ["MED-0003", "PARA-1"]

    @pytest.mark.usefixtures("catalogue")
    def test_no_match_is_an_empty_page(self, client: TestClient) -> None:
        assert fetch(client, q="zzz") == {"items": [], "next_cursor": None}

    @pytest.mark.usefixtures("catalogue")
    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("%", ["MED-0004"]),
            ("0%", ["MED-0004"]),
            ("_", ["MED-0005"]),
            ("n_c", ["MED-0005"]),
            ("t_m", []),
            ("\\", ["MED-0007"]),
            ("k\\s", ["MED-0007"]),
        ],
    )
    def test_percent_underscore_and_backslash_are_matched_literally(
        self, client: TestClient, q: str, expected: list[str]
    ) -> None:
        assert codes(fetch(client, q=q)) == expected

    @pytest.mark.usefixtures("catalogue")
    def test_surrounding_whitespace_is_ignored(self, client: TestClient) -> None:
        assert codes(fetch(client, q="  amox  ")) == ["MED-0001", "MED-0002"]

    @pytest.mark.usefixtures("catalogue")
    @pytest.mark.parametrize("q", ["", "   "])
    def test_a_blank_term_means_no_filter(self, client: TestClient, q: str) -> None:
        assert len(fetch(client, q=q)["items"]) == 8

    def test_accepts_a_term_of_100_characters_and_rejects_101(self, client: TestClient) -> None:
        assert fetch(client, q="a" * 100)["items"] == []

        body = assert_problem(
            client.get(MEDICINES, params={"q": "a" * 101}), 422, "validation_error"
        )

        assert violation_pairs(body) == [("too_long", "q")]
        assert body["violations"][0]["context"] == {"max_length": 100}

    def test_rejects_control_characters_in_the_term(self, client: TestClient) -> None:
        response = client.get(MEDICINES, params={"q": "a\x00b"})

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [("invalid_format", "q")]

    def test_paging_through_search_results_returns_exactly_the_matches(
        self, client: TestClient, make_medicine: Callable[..., Medicine]
    ) -> None:
        matching = [make_medicine(name="Cough syrup") for _ in range(23)]
        for _ in range(9):
            make_medicine(name="Aspirin")

        pages = walk(client, q="cough", limit=10)

        assert [code for page in pages for code in page] == [m.code for m in matching]
        assert [len(page) for page in pages] == [10, 10, 3]

    def test_a_cursor_is_valid_for_the_same_term_however_it_is_spelt_around(
        self, client: TestClient, make_medicine: Callable[..., Medicine]
    ) -> None:
        for _ in range(3):
            make_medicine(name="Cough syrup")
        cursor = fetch(client, q="cough", limit=1)["next_cursor"]

        assert len(fetch(client, q="  cough ", limit=1, cursor=cursor)["items"]) == 1

    def test_a_cursor_for_one_term_is_refused_for_another(
        self, client: TestClient, make_medicine: Callable[..., Medicine]
    ) -> None:
        for _ in range(3):
            make_medicine(name="Cough syrup")
        cursor = fetch(client, q="cough", limit=1)["next_cursor"]

        response = client.get(MEDICINES, params={"q": "syrup", "cursor": cursor})

        assert_problem(response, 400, "invalid_cursor")
