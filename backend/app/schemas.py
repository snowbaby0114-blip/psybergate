"""Request and response models of the API.

Request models forbid unknown fields and refuse what PostgreSQL cannot store or what would
overflow later arithmetic, so such input is a 422 at the edge and never a 500 further in. Their
validators raise PydanticCustomError with the stable violation codes (required,
invalid_format, out_of_range, ...), which app.errors passes through unchanged.
"""

import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
)
from pydantic_core import PydanticCustomError

from app.domain.rules import RuleStatus
from app.domain.timeutil import ensure_utc

MAX_QUANTITY = 1_000_000
# The rule limits are stored in an integer column.
MAX_RULE_LIMIT = 2_147_483_647
MIN_EFFECTIVE_FROM = date(1990, 1, 1)
MAX_EFFECTIVE_FROM = date(2100, 12, 31)
MIN_DISPENSED_AT = datetime(1990, 1, 1, tzinfo=UTC)
MAX_DISPENSED_AT = datetime(2100, 12, 31, 23, 59, 59, tzinfo=UTC)

# ASCII digits only: \d would also match other scripts' digits, which date.fromisoformat refuses.
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _no_control_characters(value: str) -> str:
    # Cc covers the C0/C1 controls including NUL, which PostgreSQL text cannot hold. Lone
    # surrogates, the other thing the database cannot store, never get this far: Pydantic's own
    # str validation refuses them (string_unicode).
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise PydanticCustomError("invalid_format", "Must not contain control characters.")
    return value


def _printable_ascii_without_spaces(value: str) -> str:
    if not all("!" <= char <= "~" for char in value):
        raise PydanticCustomError(
            "invalid_format", "Must contain only printable ASCII characters, without spaces."
        )
    return value


def _blank_to_none(value: str | None) -> str | None:
    return value or None


def _parse_business_date(value: object) -> date:
    if not isinstance(value, str):
        raise PydanticCustomError("invalid_type", "Must be a string formatted YYYY-MM-DD.")
    if not _DATE_PATTERN.fullmatch(value):
        raise PydanticCustomError("invalid_format", "Must be a date formatted YYYY-MM-DD.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise PydanticCustomError(
            "invalid_format", "Must be an existing calendar date formatted YYYY-MM-DD."
        ) from None
    if not MIN_EFFECTIVE_FROM <= parsed <= MAX_EFFECTIVE_FROM:
        raise PydanticCustomError(
            "out_of_range",
            "Must be between {min} and {max}.",
            {"min": MIN_EFFECTIVE_FROM.isoformat(), "max": MAX_EFFECTIVE_FROM.isoformat()},
        )
    return parsed


def _parse_instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise PydanticCustomError("invalid_type", "Must be an ISO-8601 timestamp string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise PydanticCustomError(
            "invalid_format",
            "Must be an ISO-8601 timestamp with a UTC offset, such as 2026-03-15T10:00:00+02:00.",
        ) from None
    if parsed.utcoffset() is None:
        raise PydanticCustomError(
            "invalid_format",
            "Must carry a UTC offset (Z or +02:00): a timestamp without one is ambiguous.",
        )
    # Range check before astimezone(): converting a year-9999 or year-1 value to UTC overflows,
    # while comparing aware datetimes never does.
    if not MIN_DISPENSED_AT <= parsed <= MAX_DISPENSED_AT:
        raise PydanticCustomError(
            "out_of_range",
            "Must be between {min} and {max}.",
            {
                "min": MIN_DISPENSED_AT.isoformat().replace("+00:00", "Z"),
                "max": MAX_DISPENSED_AT.isoformat().replace("+00:00", "Z"),
            },
        )
    return parsed.astimezone(UTC)


SearchText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=100),
    AfterValidator(_no_control_characters),
]
MedicineCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(_no_control_characters),
]
PatientRef = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    AfterValidator(_no_control_characters),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    AfterValidator(_printable_ascii_without_spaces),
]
AuthorisationRef = Annotated[
    Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=128),
        AfterValidator(_no_control_characters),
    ]
    | None,
    AfterValidator(_blank_to_none),
]
Quantity = Annotated[int, Field(strict=True, ge=1, le=MAX_QUANTITY)]
RuleLimit = Annotated[int, Field(strict=True, ge=1, le=MAX_RULE_LIMIT)]
BusinessDate = Annotated[date, BeforeValidator(_parse_business_date)]
InstantWithOffset = Annotated[datetime, BeforeValidator(_parse_instant)]
UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]


class RequestModel(BaseModel):
    # A typo such as "medicine" for "medicine_code" must fail loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid")


class RuleCreate(RequestModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "effective_from": "2026-11-01",
                    "max_quantity_per_dispense": 30,
                    "max_quantity_per_30_days": 60,
                    "requires_authorisation": True,
                }
            ]
        }
    )

    effective_from: BusinessDate = Field(
        description="First business day (SAST) on which the rule applies, YYYY-MM-DD."
    )
    max_quantity_per_dispense: RuleLimit
    max_quantity_per_30_days: RuleLimit
    requires_authorisation: StrictBool = False


class DispenseCreate(RequestModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "idempotency_key": "7f6c2c0e-5a49-4a1b-9f70-5b1c1f1d3a10",
                    "medicine_code": "MED-0001",
                    "patient_ref": "PT-7K2M9Q",
                    "quantity": 20,
                    "dispensed_at": "2026-03-15T10:00:00+02:00",
                    "authorisation_ref": "AUTH-123",
                }
            ]
        }
    )

    idempotency_key: IdempotencyKey = Field(
        description="Chosen by the client per intent; repeating a request with the same key and "
        "payload returns the original outcome."
    )
    medicine_code: MedicineCode
    patient_ref: PatientRef
    quantity: Quantity
    dispensed_at: InstantWithOffset | None = Field(
        default=None,
        description="ISO-8601 timestamp with a UTC offset; the server's current time if omitted. "
        "Stored and returned in UTC.",
    )
    authorisation_ref: AuthorisationRef = Field(
        default=None, description="Trimmed; a blank value counts as absent."
    )


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MedicineOut(ResponseModel):
    code: str
    name: str
    form: str
    strength_value: float
    strength_unit: str
    is_active: bool


class RuleOut(ResponseModel):
    id: int
    effective_from: date
    effective_to: date | None = Field(
        description="Exclusive: the first business day the rule no longer applies. "
        "Null means open-ended."
    )
    max_quantity_per_dispense: int
    max_quantity_per_30_days: int
    requires_authorisation: bool
    status: RuleStatus = Field(description="Relative to today's business date in SAST.")
    created_at: UtcDatetime


class MedicineDetail(MedicineOut):
    as_of: date = Field(description="Today's business date in SAST, the date current_rule is for.")
    current_rule: RuleOut | None = Field(description="The rule in force on as_of, if any.")


class RuleListOut(ResponseModel):
    items: list[RuleOut] = Field(description="The full history, ascending by effective_from.")


class RuleSupersedeResult(ResponseModel):
    rule: RuleOut = Field(description="The rule that was created.")
    superseded: RuleOut | None = Field(
        description="The rule that was cut short to make room for it, as it is now."
    )


class Page[T](ResponseModel):
    items: list[T]
    next_cursor: str | None = Field(
        description="Pass as the cursor parameter to get the next page; null on the last page."
    )


class DispenseOut(ResponseModel):
    id: int
    medicine_code: str
    medicine_name: str
    patient_ref: str
    quantity: int
    dispensed_at: UtcDatetime
    authorisation_ref: str | None
    idempotency_key: str
    rule_id: int = Field(description="The rule that governed the dispense.")
    created_at: UtcDatetime


class HealthOut(ResponseModel):
    status: Literal["ok"]


class ViolationOut(BaseModel):
    code: str = Field(description="Stable machine-readable cause, e.g. out_of_range.")
    field: str | None = Field(
        description="Request field (body field, query parameter or dotted path) the violation "
        "is about; null when it is not about one field."
    )
    message: str
    context: dict[str, Any] = Field(
        description="Machine-readable details; never needed to display."
    )


class ProblemOut(BaseModel):
    """The body of every non-2xx response (RFC 9457 plus violations)."""

    type: str = Field(examples=["urn:emguidance:problem:dispense_rejected"])
    title: str
    status: int
    detail: str
    code: str = Field(
        description="Stable machine-readable error code.", examples=["dispense_rejected"]
    )
    request_id: str = Field(description="Equals the X-Request-ID response header.")
    violations: list[ViolationOut]
    attempt_id: int | None = Field(
        default=None, description="Only on dispense_rejected: the id of the logged attempt."
    )
