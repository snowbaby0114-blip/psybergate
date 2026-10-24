"""POST /api/v1/dispenses: what a recorded dispense looks like, and which requests are not even
considered because they are malformed."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app.models import FormularyRule, Medicine
from tests.api.dispense_support import (
    DISPENSES,
    OMIT,
    REPLAYED,
    Ledger,
    payload,
    post_dispense,
)
from tests.api.support import FakeClock, assert_problem, violation_pairs

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]


@pytest.fixture
def medicine(make_medicine: MedicineFactory) -> Medicine:
    return make_medicine(code="MED-0001")


@pytest.fixture
def rule(medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
    """In force since 1 January 2026, at most 10 per dispense and 30 per 30 days."""
    return make_rule(medicine, date(2026, 1, 1))


@pytest.fixture
def ledger(session_factory: sessionmaker[Session]) -> Ledger:
    return Ledger(session_factory)


class TestRecordedDispense:
    def test_is_returned_as_a_201_with_the_documented_fields(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        response = post_dispense(client, authorisation_ref="AUTH-1")

        assert response.status_code == 201
        assert response.headers["content-type"] == "application/json"
        assert REPLAYED not in response.headers
        [stored] = ledger.dispenses()
        body = response.json()
        assert datetime.fromisoformat(body.pop("created_at")) == stored.created_at
        assert body == {
            "id": stored.id,
            "medicine_code": "MED-0001",
            "medicine_name": "Amoxil",
            "patient_ref": "PT-1",
            "quantity": 5,
            "dispensed_at": "2026-03-15T10:00:00Z",
            "authorisation_ref": "AUTH-1",
            "idempotency_key": "key-1",
            "rule_id": rule.id,
        }

    def test_is_stored_as_the_request_says(
        self, client: TestClient, medicine: Medicine, rule: FormularyRule, ledger: Ledger
    ) -> None:
        post_dispense(client, authorisation_ref="AUTH-1")

        [stored] = ledger.dispenses()
        assert (
            stored.medicine_id,
            stored.rule_id,
            stored.patient_ref,
            stored.quantity,
            stored.dispensed_at,
            stored.authorisation_ref,
            stored.idempotency_key,
        ) == (
            medicine.id,
            rule.id,
            "PT-1",
            5,
            datetime(2026, 3, 15, 10, 0, tzinfo=UTC),
            "AUTH-1",
            "key-1",
        )

    def test_without_dispensed_at_it_happens_now(
        self, client: TestClient, rule: FormularyRule, clock: FakeClock
    ) -> None:
        clock.now = datetime(2026, 6, 15, 10, 0, 7, 250000, tzinfo=UTC)

        response = post_dispense(client, dispensed_at=OMIT)

        assert response.status_code == 201
        assert response.json()["dispensed_at"] == "2026-06-15T10:00:07.250000Z"

    def test_a_null_dispensed_at_means_now_too(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        response = post_dispense(client, dispensed_at=None)

        assert response.status_code == 201
        assert response.json()["dispensed_at"] == "2026-06-15T10:00:00Z"

    @pytest.mark.parametrize(
        ("given", "returned"),
        [
            ("2026-03-15T12:00:00+02:00", "2026-03-15T10:00:00Z"),
            ("2026-03-15T05:00:00-05:00", "2026-03-15T10:00:00Z"),
            ("2026-03-15T10:00:00.123456Z", "2026-03-15T10:00:00.123456Z"),
            ("2026-03-15T10:00:00+00:00", "2026-03-15T10:00:00Z"),
        ],
    )
    def test_dispensed_at_is_returned_in_utc_whatever_offset_it_was_given_in(
        self, client: TestClient, rule: FormularyRule, given: str, returned: str
    ) -> None:
        response = post_dispense(client, dispensed_at=given)

        assert response.json()["dispensed_at"] == returned

    @pytest.mark.parametrize(
        ("given", "stored"), [("  AUTH-1 ", "AUTH-1"), ("   ", None), ("", None)]
    )
    def test_the_authorisation_ref_is_trimmed_and_a_blank_one_is_none(
        self, client: TestClient, rule: FormularyRule, given: str, stored: str | None
    ) -> None:
        response = post_dispense(client, authorisation_ref=given)

        assert response.json()["authorisation_ref"] == stored

    def test_text_fields_are_trimmed(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        response = post_dispense(client, medicine_code=" MED-0001 ", patient_ref="  PT-1\t")

        assert response.status_code == 201
        assert response.json()["patient_ref"] == "PT-1"
        assert ledger.dispenses()[0].patient_ref == "PT-1"

    def test_a_date_in_the_future_is_allowed_and_judged_by_the_rule_in_force_then(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory
    ) -> None:
        make_rule(medicine, date(2026, 1, 1), date(2026, 9, 1))
        scheduled = make_rule(medicine, date(2026, 9, 1), max_quantity_per_dispense=50)

        response = post_dispense(client, quantity=20, dispensed_at="2026-09-15T10:00:00Z")

        assert response.status_code == 201
        assert response.json()["rule_id"] == scheduled.id

    def test_a_dispense_of_one_medicine_does_not_need_the_rules_of_another(
        self, client: TestClient, rule: FormularyRule, make_medicine: MedicineFactory
    ) -> None:
        make_medicine(code="MED-0002")

        assert post_dispense(client).status_code == 201

    def test_the_bounds_of_the_supported_dispensed_at_range_are_accepted(
        self, client: TestClient, medicine: Medicine, make_rule: RuleFactory
    ) -> None:
        make_rule(medicine, date(1990, 1, 1))

        first = post_dispense(client, idempotency_key="a", dispensed_at="1990-01-01T00:00:00Z")
        last = post_dispense(client, idempotency_key="b", dispensed_at="2100-12-31T23:59:59Z")

        assert (first.status_code, last.status_code) == (201, 201)


INVALID_DISPENSED_AT = {
    "no-offset": ("2026-03-15T10:00:00", "invalid_format"),
    "date-only": ("2026-03-15", "invalid_format"),
    "free-text": ("yesterday", "invalid_format"),
    "no-such-day": ("2026-02-30T10:00:00Z", "invalid_format"),
    "number": (1773568800, "invalid_type"),
    "before-1990": ("1989-12-31T23:59:59Z", "out_of_range"),
    "after-2100": ("2101-01-01T00:00:00Z", "out_of_range"),
    "year-1": ("0001-01-01T00:00:00Z", "out_of_range"),
    "year-9999": ("9999-12-31T23:59:59Z", "out_of_range"),
    "year-1-far-east": ("0001-01-01T00:00:00+23:59", "out_of_range"),
    "year-9999-far-west": ("9999-12-31T23:59:59-23:59", "out_of_range"),
    "just-before-1990-in-utc": ("1990-01-01T00:00:00+00:01", "out_of_range"),
    "just-after-2100-in-utc": ("2100-12-31T23:59:59-00:01", "out_of_range"),
}

CONTROL_CHARACTERS = {
    "nul": "a\x00b",
    "escape": "a\x1bb",
    "newline": "a\nb",
    "tab": "a\tb",
    "unit-separator": "a\x1fb",
    "delete": "a\x7fb",
    "c1-control": "a\x85b",
}


class TestMalformedRequests:
    """None of these is a business attempt: each is a 422 validation_error, never a 500, and
    leaves nothing in the dispenses or the attempt log."""

    @pytest.fixture(autouse=True)
    def _a_medicine_with_a_rule(self, rule: FormularyRule) -> None:
        return None

    @pytest.mark.parametrize(
        ("value", "code"), INVALID_DISPENSED_AT.values(), ids=INVALID_DISPENSED_AT.keys()
    )
    def test_dispensed_at_must_be_a_timestamp_with_an_offset_in_the_supported_range(
        self, client: TestClient, ledger: Ledger, value: Any, code: str
    ) -> None:
        response = post_dispense(client, dispensed_at=value)

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [(code, "dispensed_at")]
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])

    @pytest.mark.parametrize("text", CONTROL_CHARACTERS.values(), ids=CONTROL_CHARACTERS.keys())
    @pytest.mark.parametrize("field", ["patient_ref", "medicine_code", "authorisation_ref"])
    def test_free_text_may_not_contain_control_characters(
        self, client: TestClient, ledger: Ledger, field: str, text: str
    ) -> None:
        response = post_dispense(client, **{field: text})

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [("invalid_format", field)]
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])

    @pytest.mark.parametrize(
        "key",
        ["", "   ", "with space", "tab\there", "kéy", "\x00", "x" * 129],
        ids=["empty", "blank", "space", "tab", "non-ascii", "nul", "too-long"],
    )
    def test_the_idempotency_key_is_printable_ascii_of_1_to_128_characters(
        self, client: TestClient, ledger: Ledger, key: str
    ) -> None:
        response = post_dispense(client, idempotency_key=key)

        body = assert_problem(response, 422, "validation_error")
        assert [field for _, field in violation_pairs(body)] == ["idempotency_key"]
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])

    def test_the_longest_idempotency_key_is_accepted(self, client: TestClient) -> None:
        assert post_dispense(client, idempotency_key="k" * 128).status_code == 201

    @pytest.mark.parametrize(
        ("value", "code"),
        [
            (0, "out_of_range"),
            (-1, "out_of_range"),
            (1_000_001, "out_of_range"),
            ("5", "invalid_type"),
            (5.5, "invalid_type"),
            (5.0, "invalid_type"),
            (True, "invalid_type"),
            (None, "invalid_type"),
            ([5], "invalid_type"),
        ],
    )
    def test_quantity_is_an_integer_from_1_to_1_000_000(
        self, client: TestClient, ledger: Ledger, value: Any, code: str
    ) -> None:
        response = post_dispense(client, quantity=value)

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [(code, "quantity")]
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])

    @pytest.mark.parametrize(
        ("field", "value", "code"),
        [
            ("medicine_code", "", "too_short"),
            ("medicine_code", "x" * 65, "too_long"),
            ("medicine_code", 7, "invalid_type"),
            ("patient_ref", "  ", "too_short"),
            ("patient_ref", "x" * 129, "too_long"),
            ("authorisation_ref", "x" * 129, "too_long"),
            ("authorisation_ref", 7, "invalid_type"),
        ],
    )
    def test_text_fields_have_their_length_limits(
        self, client: TestClient, ledger: Ledger, field: str, value: Any, code: str
    ) -> None:
        response = post_dispense(client, **{field: value})

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [(code, field)]
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])

    def test_the_longest_texts_are_accepted(self, client: TestClient) -> None:
        response = post_dispense(client, patient_ref="p" * 128, authorisation_ref="a" * 128)

        assert response.status_code == 201

    def test_every_problem_is_reported_at_once(self, client: TestClient) -> None:
        response = client.post(
            DISPENSES,
            json={
                "idempotency_key": "",
                "medicine": "MED-0001",
                "patient_ref": "a\x00b",
                "quantity": 0,
                "dispensed_at": "soon",
            },
        )

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [
            ("too_short", "idempotency_key"),
            ("required", "medicine_code"),
            ("invalid_format", "patient_ref"),
            ("out_of_range", "quantity"),
            ("invalid_format", "dispensed_at"),
            ("unknown_field", "medicine"),
        ]

    @pytest.mark.parametrize(
        "name", ["idempotency_key", "medicine_code", "patient_ref", "quantity"]
    )
    def test_the_required_fields_are_required(
        self, client: TestClient, ledger: Ledger, name: str
    ) -> None:
        response = post_dispense(client, **{name: OMIT})

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [("required", name)]
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])

    def test_a_misspelt_field_is_refused_instead_of_ignored(self, client: TestClient) -> None:
        response = client.post(
            DISPENSES, json=payload(medicine_code=OMIT) | {"medicine": "MED-0001"}
        )

        body = assert_problem(response, 422, "validation_error")
        assert violation_pairs(body) == [
            ("required", "medicine_code"),
            ("unknown_field", "medicine"),
        ]

    def test_a_missing_body_is_one_required_violation_without_a_field(
        self, client: TestClient
    ) -> None:
        body = assert_problem(client.post(DISPENSES), 422, "validation_error")

        assert violation_pairs(body) == [("required", None)]

    @pytest.mark.parametrize("content", [b"{", b"[]", b'"text"', b"null"])
    def test_a_body_that_is_not_a_json_object_is_refused(
        self, client: TestClient, content: bytes
    ) -> None:
        response = client.post(
            DISPENSES, content=content, headers={"content-type": "application/json"}
        )

        assert_problem(response, 422, "validation_error")

    def test_an_escaped_lone_surrogate_is_refused_instead_of_crashing_the_database_driver(
        self, client: TestClient, ledger: Ledger
    ) -> None:
        response = client.post(
            DISPENSES,
            content=b'{"idempotency_key":"k","medicine_code":"MED-0001",'
            b'"patient_ref":"a\\ud800b","quantity":1}',
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 422
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])

    def test_input_values_are_never_echoed_back(self, client: TestClient) -> None:
        response = post_dispense(client, patient_ref="hunter2\x00", dispensed_at="hunter2-secret")

        assert response.status_code == 422
        assert "hunter2" not in response.text


class TestPublishedContract:
    def test_the_openapi_document_describes_both_endpoints_and_the_replay_header(
        self, client: TestClient
    ) -> None:
        paths = client.get("/api/openapi.json").json()["paths"]["/api/v1/dispenses"]

        assert set(paths["post"]["responses"]) == {"201", "422", "503"}
        assert set(paths["get"]["responses"]) == {"200", "400", "422"}
        for status in ("201", "422"):
            assert list(paths["post"]["responses"][status]["headers"]) == ["Idempotency-Replayed"]
        assert {parameter["name"] for parameter in paths["get"]["parameters"]} == {
            "patient_ref",
            "medicine",
            "limit",
            "cursor",
        }
