"""Input hygiene of the request models and the wire format of the response models."""

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel, ValidationError

from app import errors
from app.schemas import DispenseCreate, DispenseOut, RuleCreate

DISPENSE: dict[str, Any] = {
    "idempotency_key": "key-1",
    "medicine_code": "MED-0001",
    "patient_ref": "PT-1",
    "quantity": 20,
}
RULE: dict[str, Any] = {
    "effective_from": "2026-09-01",
    "max_quantity_per_dispense": 30,
    "max_quantity_per_30_days": 60,
}


def problems(model: type[BaseModel], data: dict[str, Any]) -> list[tuple[str, str | None]]:
    """The (code, field) of every violation the API would report for `data`."""
    with pytest.raises(ValidationError) as raised:
        model.model_validate(data)
    return [
        (violation.code, violation.field)
        for violation in (
            errors.violation_from_validation_error(error)
            for error in raised.value.errors(include_url=False)
        )
    ]


class TestDispenseCreate:
    def test_a_minimal_request_leaves_the_optional_fields_empty(self) -> None:
        request = DispenseCreate.model_validate(DISPENSE)

        assert request.dispensed_at is None
        assert request.authorisation_ref is None

    def test_strings_are_trimmed(self) -> None:
        request = DispenseCreate.model_validate(
            DISPENSE
            | {
                "idempotency_key": "  key-1 ",
                "medicine_code": " MED-0001\t",
                "patient_ref": "\nPT-1 ",
                "authorisation_ref": " AUTH-9 ",
            }
        )

        assert request.idempotency_key == "key-1"
        assert request.medicine_code == "MED-0001"
        assert request.patient_ref == "PT-1"
        assert request.authorisation_ref == "AUTH-9"

    @pytest.mark.parametrize("blank", ["", " ", " \t\n", None])
    def test_a_blank_authorisation_reference_means_none(self, blank: str | None) -> None:
        assert (
            DispenseCreate.model_validate(DISPENSE | {"authorisation_ref": blank}).authorisation_ref
            is None
        )

    @pytest.mark.parametrize(
        ("sent", "instant"),
        [
            ("2026-03-15T10:00:00+02:00", datetime(2026, 3, 15, 8, 0, tzinfo=UTC)),
            ("2026-03-15T08:00:00Z", datetime(2026, 3, 15, 8, 0, tzinfo=UTC)),
            ("2026-03-15T08:00:00+00:00", datetime(2026, 3, 15, 8, 0, tzinfo=UTC)),
            ("2026-03-15T23:30:00-05:00", datetime(2026, 3, 16, 4, 30, tzinfo=UTC)),
            ("2026-03-15 10:00:00+02:00", datetime(2026, 3, 15, 8, 0, tzinfo=UTC)),
            ("2026-03-15T08:00:00.123456Z", datetime(2026, 3, 15, 8, 0, 0, 123456, tzinfo=UTC)),
            ("1990-01-01T00:00:00Z", datetime(1990, 1, 1, tzinfo=UTC)),
            ("2100-12-31T23:59:59Z", datetime(2100, 12, 31, 23, 59, 59, tzinfo=UTC)),
        ],
    )
    def test_dispensed_at_is_normalised_to_utc(self, sent: str, instant: datetime) -> None:
        parsed = DispenseCreate.model_validate(DISPENSE | {"dispensed_at": sent}).dispensed_at

        assert parsed == instant
        assert parsed is not None
        assert parsed.utcoffset() == timedelta(0)
        assert parsed.tzinfo is UTC

    def test_the_edges_of_every_limit_are_accepted(self) -> None:
        request = DispenseCreate.model_validate(
            {
                "idempotency_key": "k" * 128,
                "medicine_code": "m" * 64,
                "patient_ref": "p" * 128,
                "quantity": 1_000_000,
                "authorisation_ref": "a" * 128,
            }
        )

        assert request.quantity == 1_000_000
        assert DispenseCreate.model_validate(DISPENSE | {"quantity": 1}).quantity == 1

    @pytest.mark.parametrize(
        ("changes", "expected"),
        [
            ({"quantity": 0}, [("out_of_range", "quantity")]),
            ({"quantity": -1}, [("out_of_range", "quantity")]),
            ({"quantity": 1_000_001}, [("out_of_range", "quantity")]),
            ({"quantity": 10**30}, [("out_of_range", "quantity")]),
            ({"quantity": "5"}, [("invalid_type", "quantity")]),
            ({"quantity": 5.0}, [("invalid_type", "quantity")]),
            ({"quantity": True}, [("invalid_type", "quantity")]),
            ({"quantity": None}, [("invalid_type", "quantity")]),
            ({"idempotency_key": ""}, [("too_short", "idempotency_key")]),
            ({"idempotency_key": "   "}, [("too_short", "idempotency_key")]),
            ({"idempotency_key": "k" * 129}, [("too_long", "idempotency_key")]),
            ({"idempotency_key": "two words"}, [("invalid_format", "idempotency_key")]),
            ({"idempotency_key": "tab\there"}, [("invalid_format", "idempotency_key")]),
            ({"idempotency_key": "clé"}, [("invalid_format", "idempotency_key")]),
            ({"idempotency_key": "nul\x00"}, [("invalid_format", "idempotency_key")]),
            ({"idempotency_key": 12345}, [("invalid_type", "idempotency_key")]),
            ({"medicine_code": ""}, [("too_short", "medicine_code")]),
            ({"medicine_code": "m" * 65}, [("too_long", "medicine_code")]),
            ({"medicine_code": "MED\x00"}, [("invalid_format", "medicine_code")]),
            ({"medicine_code": None}, [("invalid_type", "medicine_code")]),
            ({"patient_ref": ""}, [("too_short", "patient_ref")]),
            ({"patient_ref": "p" * 129}, [("too_long", "patient_ref")]),
            ({"patient_ref": "a\x00b"}, [("invalid_format", "patient_ref")]),
            ({"patient_ref": "a\nb"}, [("invalid_format", "patient_ref")]),
            ({"patient_ref": "a\x85b"}, [("invalid_format", "patient_ref")]),
            ({"patient_ref": "a\ud800b"}, [("invalid_format", "patient_ref")]),
            ({"authorisation_ref": "a" * 129}, [("too_long", "authorisation_ref")]),
            ({"authorisation_ref": "a\x00b"}, [("invalid_format", "authorisation_ref")]),
            ({"authorisation_ref": 7}, [("invalid_type", "authorisation_ref")]),
            ({"medicine": "MED-0001"}, [("unknown_field", "medicine")]),
        ],
    )
    def test_refuses_what_the_database_or_the_rules_cannot_take(
        self, changes: dict[str, Any], expected: list[tuple[str, str]]
    ) -> None:
        assert problems(DispenseCreate, DISPENSE | changes) == expected

    @pytest.mark.parametrize(
        ("sent", "expected"),
        [
            ("2026-03-15T10:00:00", ("invalid_format", "dispensed_at")),  # no offset: ambiguous
            ("2026-03-15", ("invalid_format", "dispensed_at")),
            ("tomorrow", ("invalid_format", "dispensed_at")),
            ("", ("invalid_format", "dispensed_at")),
            (1773568800, ("invalid_type", "dispensed_at")),
            (True, ("invalid_type", "dispensed_at")),
            ("1989-12-31T23:59:59Z", ("out_of_range", "dispensed_at")),
            ("2101-01-01T00:00:00Z", ("out_of_range", "dispensed_at")),
            ("2100-12-31T23:59:59.5Z", ("out_of_range", "dispensed_at")),
            # Converting these to UTC would overflow datetime: they must be refused first.
            ("9999-12-31T23:59:59-01:00", ("out_of_range", "dispensed_at")),
            ("0001-01-01T00:00:00+02:00", ("out_of_range", "dispensed_at")),
            ("1990-01-01T00:00:00+02:00", ("out_of_range", "dispensed_at")),
        ],
    )
    def test_dispensed_at_must_be_a_real_instant_in_range(
        self, sent: object, expected: tuple[str, str]
    ) -> None:
        assert problems(DispenseCreate, DISPENSE | {"dispensed_at": sent}) == [expected]

    def test_reports_every_problem_at_once_in_field_order(self) -> None:
        data = {
            "idempotency_key": "",
            "medicine_code": "",
            "patient_ref": "",
            "quantity": 0,
            "dispensed_at": "2026-03-15T10:00:00",
            "authorisation_ref": "x" * 129,
            "oops": 1,
        }

        assert problems(DispenseCreate, data) == [
            ("too_short", "idempotency_key"),
            ("too_short", "medicine_code"),
            ("too_short", "patient_ref"),
            ("out_of_range", "quantity"),
            ("invalid_format", "dispensed_at"),
            ("too_long", "authorisation_ref"),
            ("unknown_field", "oops"),
        ]

    def test_the_required_fields_are_named(self) -> None:
        assert problems(DispenseCreate, {}) == [
            ("required", "idempotency_key"),
            ("required", "medicine_code"),
            ("required", "patient_ref"),
            ("required", "quantity"),
        ]


class TestRuleCreate:
    def test_requires_authorisation_defaults_to_false(self) -> None:
        assert RuleCreate.model_validate(RULE).requires_authorisation is False

    def test_a_valid_request_is_parsed(self) -> None:
        request = RuleCreate.model_validate(RULE | {"requires_authorisation": True})

        assert request.effective_from == date(2026, 9, 1)
        assert request.requires_authorisation is True

    def test_the_largest_limit_is_the_integer_column_maximum(self) -> None:
        request = RuleCreate.model_validate(RULE | {"max_quantity_per_30_days": 2**31 - 1})

        assert request.max_quantity_per_30_days == 2**31 - 1


class TestWireFormat:
    @staticmethod
    def dispense_out(dispensed_at: datetime) -> DispenseOut:
        return DispenseOut(
            id=1,
            medicine_code="MED-0001",
            medicine_name="Amoxil",
            patient_ref="PT-1",
            quantity=20,
            dispensed_at=dispensed_at,
            authorisation_ref=None,
            idempotency_key="key-1",
            rule_id=3,
            created_at=datetime(2026, 3, 15, 10, 0, 4, tzinfo=UTC),
        )

    def test_instants_are_utc_with_a_z_suffix(self) -> None:
        sast = ZoneInfo("Africa/Johannesburg")

        body = self.dispense_out(datetime(2026, 3, 15, 12, 0, tzinfo=sast)).model_dump(mode="json")

        assert body["dispensed_at"] == "2026-03-15T10:00:00Z"
        assert body["created_at"] == "2026-03-15T10:00:04Z"

    def test_microseconds_appear_only_when_they_are_not_zero(self) -> None:
        body = self.dispense_out(datetime(2026, 3, 15, 10, 0, 0, 123456, tzinfo=UTC)).model_dump(
            mode="json"
        )

        assert body["dispensed_at"] == "2026-03-15T10:00:00.123456Z"

    def test_an_instant_without_an_offset_is_a_bug_not_a_value(self) -> None:
        with pytest.raises(ValidationError):
            self.dispense_out(datetime(2026, 3, 15, 10, 0))

    def test_an_instant_with_a_fixed_offset_is_converted(self) -> None:
        offset = timezone(timedelta(hours=-5))

        body = self.dispense_out(datetime(2026, 3, 15, 5, 0, tzinfo=offset)).model_dump(mode="json")

        assert body["dispensed_at"] == "2026-03-15T10:00:00Z"
