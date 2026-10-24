"""The error contract: RFC 9457 problem details plus a uniform `violations` list.

Every non-2xx response of the API is built by `problem_response`, whichever handler produced it,
so clients see exactly one shape. Services raise `ProblemError` through the
constructors below; the handlers registered by `register_exception_handlers` translate framework,
database and unexpected errors into the same shape.
"""

import http
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, Final

import psycopg
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api.request_id import REQUEST_ID_HEADER, request_id_of
from app.domain.dispense import Violation
from app.domain.timeutil import TZ

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
PROBLEM_TYPE_PREFIX: Final = "urn:emguidance:problem:"

# One constant title per code, as RFC 9457 asks; the per-occurrence text goes in `detail`.
PROBLEM_TITLES: Final[dict[str, str]] = {
    "validation_error": "Validation failed",
    "dispense_rejected": "Dispense rejected",
    "idempotency_key_reuse": "Idempotency key reused",
    "invalid_cursor": "Invalid cursor",
    "medicine_not_found": "Medicine not found",
    "not_found": "Not found",
    "method_not_allowed": "Method not allowed",
    "rule_conflict": "Rule conflict",
    "resource_busy": "Resource busy",
    "service_unavailable": "Service unavailable",
    "internal_error": "Internal server error",
    "http_error": "HTTP error",
}

_OVERLAP_CONSTRAINT: Final = "ex_formulary_rules_no_overlap"
_SQLSTATE_EXCLUSION_VIOLATION: Final = "23P01"
_SQLSTATE_LOCK_NOT_AVAILABLE: Final = "55P03"
_SQLSTATE_DEADLOCK_DETECTED: Final = "40P01"
_SQLSTATE_QUERY_CANCELED: Final = "57014"

_log = logging.getLogger(__name__)


class ProblemError(Exception):
    """An error reported to the client as a problem+json response with the given status."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        violations: Sequence[Violation] = (),
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.status = status
        self.code = code
        self.title = PROBLEM_TITLES[code]
        self.detail = detail
        self.violations = tuple(violations)
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _iso_utc(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sast_text(instant: datetime) -> str:
    return f"{instant.astimezone(TZ):%Y-%m-%d %H:%M} SAST"


def validation_error(violations: Sequence[Violation]) -> ProblemError:
    return ProblemError(
        422,
        "validation_error",
        f"The request is invalid: {_count(len(violations), 'problem')} found.",
        violations=violations,
    )


def invalid_cursor() -> ProblemError:
    # The reason is deliberately not passed on: the pure cursor codec words it from the token the
    # client sent, and error bodies never echo client input.
    violation = Violation(
        code="invalid_cursor",
        field="cursor",
        message=(
            "The cursor is malformed or was issued for different search parameters. "
            "Use a next_cursor value from a previous response to the same query."
        ),
        context={},
    )
    return ProblemError(
        400, "invalid_cursor", "The cursor is not valid for this request.", violations=[violation]
    )


def medicine_not_found(code: str) -> ProblemError:
    return ProblemError(404, "medicine_not_found", f"No medicine with code {code!r} exists.")


def rule_conflict(violation: Violation) -> ProblemError:
    return ProblemError(409, "rule_conflict", violation.message, violations=[violation])


# The causes a rule_conflict can carry besides rule_already_starts_on_date, which the pure
# supersede planner reports itself.
def rule_period_overlap() -> Violation:
    return Violation(
        code="rule_period_overlap",
        field="effective_from",
        message="The new rule's period overlaps another rule of this medicine.",
        context={},
    )


def rule_would_alter_recorded_dispenses(
    *, effective_from: date, count: int, earliest: datetime, latest: datetime
) -> Violation:
    return Violation(
        code="rule_would_alter_recorded_dispenses",
        field="effective_from",
        message=(
            f"A rule starting on {effective_from.isoformat()} would govern "
            f"{_count(count, 'dispense')} already recorded under the rule in force, between "
            f"{_sast_text(earliest)} and {_sast_text(latest)}. "
            "Recorded dispenses are never re-governed."
        ),
        context={
            "dispense_count": count,
            "earliest_dispensed_at": _iso_utc(earliest),
            "latest_dispensed_at": _iso_utc(latest),
        },
    )


def dispense_rejected(violations: Sequence[Violation], attempt_id: int) -> ProblemError:
    return ProblemError(
        422,
        "dispense_rejected",
        f"The dispense was rejected for {_count(len(violations), 'reason')}.",
        violations=violations,
        extra={"attempt_id": attempt_id},
    )


def idempotency_key_reuse() -> ProblemError:
    violation = Violation(
        code="idempotency_key_reuse",
        field="idempotency_key",
        message=(
            "This idempotency key was already used with a different request. "
            "Use a new key for a new request."
        ),
        context={},
    )
    return ProblemError(422, "idempotency_key_reuse", violation.message, violations=[violation])


def resource_busy() -> ProblemError:
    return ProblemError(
        503,
        "resource_busy",
        "Another request is working on the same data. Retry shortly.",
        headers={"Retry-After": "1"},
    )


def service_unavailable(detail: str) -> ProblemError:
    return ProblemError(503, "service_unavailable", detail)


def internal_error() -> ProblemError:
    return ProblemError(
        500,
        "internal_error",
        "An unexpected error occurred. Quote the request id when reporting it.",
    )


def problem_body(problem: ProblemError, request_id: str) -> dict[str, Any]:
    return {
        "type": f"{PROBLEM_TYPE_PREFIX}{problem.code}",
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
        "code": problem.code,
        "request_id": request_id,
        "violations": [
            {
                "code": violation.code,
                "field": violation.field,
                "message": violation.message,
                "context": violation.context,
            }
            for violation in problem.violations
        ],
        **problem.extra,
    }


def problem_response(request: Request, problem: ProblemError) -> JSONResponse:
    request_id = request_id_of(request)
    return JSONResponse(
        problem_body(problem, request_id),
        status_code=problem.status,
        # The header is set here as well as by RequestIdMiddleware: Starlette sends the 500 for an
        # unhandled exception from ServerErrorMiddleware, which wraps the middleware stack.
        headers={**problem.headers, REQUEST_ID_HEADER: request_id},
        media_type=PROBLEM_MEDIA_TYPE,
    )


# Codes a client can rely on in validation violations. Validators of our own models
# raise these directly through PydanticCustomError.
_VALIDATION_CODES: Final = frozenset(
    {
        "required",
        "invalid_type",
        "invalid_format",
        "out_of_range",
        "too_short",
        "too_long",
        "unknown_field",
        "invalid_json",
        "invalid_value",
    }
)

_CODE_BY_PYDANTIC_TYPE: Final = {
    "missing": "required",
    "json_invalid": "invalid_json",
    "extra_forbidden": "unknown_field",
    "string_too_short": "too_short",
    "too_short": "too_short",
    "string_too_long": "too_long",
    "too_long": "too_long",
    "greater_than": "out_of_range",
    "greater_than_equal": "out_of_range",
    "less_than": "out_of_range",
    "less_than_equal": "out_of_range",
    "int_parsing_size": "out_of_range",
    "string_type": "invalid_type",
    "int_type": "invalid_type",
    "float_type": "invalid_type",
    "bool_type": "invalid_type",
    "dict_type": "invalid_type",
    "list_type": "invalid_type",
    "model_type": "invalid_type",
    "model_attributes_type": "invalid_type",
    "date_type": "invalid_type",
    "datetime_type": "invalid_type",
    "int_parsing": "invalid_format",
    "int_from_float": "invalid_format",
    "float_parsing": "invalid_format",
    "bool_parsing": "invalid_format",
    "date_parsing": "invalid_format",
    "date_from_datetime_parsing": "invalid_format",
    "datetime_parsing": "invalid_format",
    "string_pattern_mismatch": "invalid_format",
    "string_unicode": "invalid_format",
}

# Only constraint values taken from the schema may reach the client, never anything derived from
# the input: Pydantic's `ctx` can also hold exception objects and offending values.
_CONTEXT_KEYS: Final = frozenset(
    {"min_length", "max_length", "gt", "ge", "lt", "le", "min", "max", "pattern"}
)

_LOCATION_PREFIXES: Final = frozenset({"body", "query", "path", "header", "cookie"})


def _field_from_location(loc: Sequence[object]) -> str | None:
    parts = list(loc)
    if parts and parts[0] in _LOCATION_PREFIXES:
        parts = parts[1:]
    return ".".join(str(part) for part in parts) or None


def _safe_context(ctx: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (ctx or {}).items()
        if key in _CONTEXT_KEYS and isinstance(value, str | int | float)
    }


def violation_from_validation_error(error: Mapping[str, Any]) -> Violation:
    """Translate one Pydantic error into a violation. Pydantic's `input` is never read."""
    error_type: str = error["type"]
    loc: Sequence[object] = error["loc"]
    code = (
        error_type
        if error_type in _VALIDATION_CODES
        else _CODE_BY_PYDANTIC_TYPE.get(error_type, "invalid_value")
    )
    # Messages of the types listed above and of our own validators are fixed templates; any other
    # type (value_error, assertion_error, ...) can carry arbitrary text, so it gets a generic one.
    known = error_type in _VALIDATION_CODES or error_type in _CODE_BY_PYDANTIC_TYPE
    message: str = error["msg"] if known else "The value is not valid."
    field = _field_from_location(loc)
    context = _safe_context(error.get("ctx"))

    if tuple(loc) == ("body",):
        message = (
            "A JSON request body is required."
            if code == "required"
            else "The request body must be a JSON object sent as application/json."
        )
    if error_type == "json_invalid":
        # FastAPI puts the character offset of the syntax error where a field name would be.
        field = None
        reason = (error.get("ctx") or {}).get("error", "syntax error")
        message = f"The request body is not valid JSON: {reason}."
        position = loc[-1]
        context = {"position": position} if isinstance(position, int) else {}
    return Violation(code=code, field=field, message=message, context=context)


def _sqlstate(exc: sa_exc.SQLAlchemyError) -> str | None:
    orig = getattr(exc, "orig", None)
    return orig.sqlstate if isinstance(orig, psycopg.Error) else None


def constraint_name(exc: sa_exc.SQLAlchemyError) -> str | None:
    """The constraint a database error is about, when it names one."""
    orig = getattr(exc, "orig", None)
    return orig.diag.constraint_name if isinstance(orig, psycopg.Error) else None


def problem_for_database_error(exc: sa_exc.SQLAlchemyError) -> ProblemError | None:
    """The client-facing problem for a database error we expect, else None (a bug: 500)."""
    sqlstate = _sqlstate(exc)
    if sqlstate == _SQLSTATE_EXCLUSION_VIOLATION and constraint_name(exc) == _OVERLAP_CONSTRAINT:
        # The planner makes this unreachable under the medicine lock; the constraint is the
        # backstop that turns a missed case into a clean conflict instead of corrupt history.
        return rule_conflict(rule_period_overlap())
    # A deadlock is answered like a lock timeout: PostgreSQL rolled this transaction back so the
    # other one could finish, and the request can simply be sent again. The lock order of the
    # dispense transaction makes one unlikely but not impossible, as two advisory keys can collide.
    if sqlstate in (_SQLSTATE_LOCK_NOT_AVAILABLE, _SQLSTATE_DEADLOCK_DETECTED):
        return resource_busy()
    if sqlstate == _SQLSTATE_QUERY_CANCELED:
        return service_unavailable("The database took too long to answer. Retry shortly.")
    # OperationalError covers refused, dropped and timed-out connections; TimeoutError is the
    # connection pool running dry. Both mean "the environment, not this request".
    if isinstance(exc, sa_exc.OperationalError | sa_exc.TimeoutError) or getattr(
        exc, "connection_invalidated", False
    ):
        return service_unavailable("The database is unavailable. Retry shortly.")
    return None


def _problem_for_http_exception(exc: StarletteHTTPException) -> ProblemError:
    if exc.status_code == 404:
        return ProblemError(404, "not_found", "No endpoint matches the requested path.")
    if exc.status_code == 405:
        return ProblemError(
            405,
            "method_not_allowed",
            "The endpoint does not support the requested method.",
            headers=exc.headers,
        )
    try:
        phrase = http.HTTPStatus(exc.status_code).phrase
    except ValueError:
        phrase = "HTTP error"
    return ProblemError(exc.status_code, "http_error", phrase, headers=exc.headers)


# The handlers are coroutines although they never await: Starlette runs a plain function in the
# thread pool, a pointless hop (and a failure mode when the pool is exhausted) for work that only
# builds a response.
async def problem_error_handler(request: Request, exc: ProblemError) -> JSONResponse:
    return problem_response(request, exc)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    violations = [violation_from_validation_error(error) for error in exc.errors()]
    # Malformed requests are not business attempts, so the application log is their only record.
    # The path is logged with %r: it is decoded client input and may hold a newline.
    _log.info(
        "request_id=%s %s %r rejected as invalid: %s",
        request_id_of(request),
        request.method,
        request.url.path,
        [(violation.field, violation.code) for violation in violations],
    )
    return problem_response(request, validation_error(violations))


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return problem_response(request, _problem_for_http_exception(exc))


async def database_error_handler(request: Request, exc: sa_exc.SQLAlchemyError) -> JSONResponse:
    problem = problem_for_database_error(exc)
    if problem is None:
        return await unhandled_exception_handler(request, exc)
    # Class and SQLSTATE only: the message of a SQLAlchemy error embeds the statement and its
    # parameters.
    _log.warning(
        "request_id=%s %s %r database error %s (sqlstate %s) answered as %s",
        request_id_of(request),
        request.method,
        request.url.path,
        type(exc).__name__,
        _sqlstate(exc),
        problem.code,
    )
    return problem_response(request, problem)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    _log.error(
        "request_id=%s %s %r failed with an unhandled exception",
        request_id_of(request),
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return problem_response(request, internal_error())


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, problem_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(sa_exc.SQLAlchemyError, database_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
