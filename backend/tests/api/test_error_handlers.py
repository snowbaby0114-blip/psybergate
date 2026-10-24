"""How failures become problem+json: framework errors, expected database errors by SQLSTATE,
service problems and plain bugs."""

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app import errors
from app.api.deps import SessionDep
from app.config import Settings
from app.domain.dispense import Violation
from app.main import create_app
from app.models import FormularyRule, Medicine
from tests.api.support import FakeClock, assert_problem, violation_pairs

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]


def add_route(app: FastAPI, endpoint: Callable[..., Any]) -> str:
    path = f"/_test/{endpoint.__name__}"
    app.add_api_route(path, endpoint)
    return path


def add_sql_route(app: FastAPI, name: str, run: Callable[[Session], None]) -> str:
    """A route that runs `run` in a transaction, to put a real database error through the
    application's handlers."""

    def endpoint(session: SessionDep) -> None:
        with session.begin():
            run(session)

    endpoint.__name__ = name
    return add_route(app, endpoint)


class TestFrameworkErrors:
    def test_an_unknown_path_is_a_404_problem(self, client: TestClient) -> None:
        body = assert_problem(client.get("/api/v1/nowhere"), 404, "not_found")

        assert body["violations"] == []

    def test_a_trailing_slash_is_not_redirected(self, client: TestClient) -> None:
        response = client.get("/api/v1/medicines/", follow_redirects=False)

        assert_problem(response, 404, "not_found")

    def test_a_method_the_path_does_not_support_is_a_405_naming_the_allowed_ones(
        self, client: TestClient
    ) -> None:
        response = client.delete("/api/v1/medicines")

        assert_problem(response, 405, "method_not_allowed")
        assert response.headers["allow"] == "GET"

    def test_any_other_http_error_keeps_its_status_and_headers_under_a_generic_code(
        self, app: FastAPI, client: TestClient
    ) -> None:
        def teapot() -> None:
            raise HTTPException(418, detail={"internal": "not for clients"}, headers={"X-Tea": "1"})

        response = client.get(add_route(app, teapot))

        body = assert_problem(response, 418, "http_error")
        assert body["detail"] == "I'm a Teapot"
        assert response.headers["x-tea"] == "1"
        assert "internal" not in response.text


class TestProblemErrors:
    """The constructors services use to raise problems."""

    def test_a_rejected_dispense_lists_every_violation_and_names_its_attempt(
        self, app: FastAPI, client: TestClient
    ) -> None:
        def rejected() -> None:
            raise errors.dispense_rejected(
                [
                    Violation(
                        "max_quantity_per_dispense_exceeded", "quantity", "Too many.", {"limit": 1}
                    ),
                    Violation("authorisation_required", "authorisation_ref", "Needs auth.", {}),
                ],
                attempt_id=41,
            )

        response = client.get(add_route(app, rejected))

        body = assert_problem(
            response, 422, "dispense_rejected", extra_keys=frozenset({"attempt_id"})
        )
        assert body["attempt_id"] == 41
        assert body["detail"] == "The dispense was rejected for 2 reasons."
        assert body["violations"] == [
            {
                "code": "max_quantity_per_dispense_exceeded",
                "field": "quantity",
                "message": "Too many.",
                "context": {"limit": 1},
            },
            {
                "code": "authorisation_required",
                "field": "authorisation_ref",
                "message": "Needs auth.",
                "context": {},
            },
        ]

    def test_reusing_an_idempotency_key_points_at_the_key(
        self, app: FastAPI, client: TestClient
    ) -> None:
        def reused() -> None:
            raise errors.idempotency_key_reuse()

        body = assert_problem(client.get(add_route(app, reused)), 422, "idempotency_key_reuse")

        assert violation_pairs(body) == [("idempotency_key_reuse", "idempotency_key")]

    def test_a_busy_resource_asks_the_client_to_retry_in_a_second(
        self, app: FastAPI, client: TestClient
    ) -> None:
        def busy() -> None:
            raise errors.resource_busy()

        response = client.get(add_route(app, busy))

        assert_problem(response, 503, "resource_busy")
        assert response.headers["retry-after"] == "1"


class TestFailureLogging:
    def test_control_characters_in_the_path_are_escaped_in_the_log(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="app.errors")

        response = client.get("/api/v1/medicines/a%1B[31mred")

        assert response.status_code == 422
        [record] = [r for r in caplog.records if r.name == "app.errors"]
        assert "\x1b" not in record.getMessage()
        assert "\\x1b" in record.getMessage()

    def test_a_rejected_request_is_logged_with_its_fields_and_codes_but_not_its_values(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="app.errors")

        response = client.post(
            "/api/v1/medicines/MED-0001/rules",
            json={"effective_from": "hunter2-secret", "max_quantity_per_dispense": 0},
        )

        [record] = [r for r in caplog.records if r.name == "app.errors"]
        assert response.json()["request_id"] in record.getMessage()
        assert "('effective_from', 'invalid_format')" in record.getMessage()
        assert "hunter2" not in record.getMessage()


class TestUnhandledErrors:
    def test_a_bug_is_a_generic_500_that_leaks_nothing_and_is_logged_with_the_request_id(
        self, app: FastAPI, error_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        def bug() -> None:
            raise RuntimeError("password=hunter2 in /srv/app/secrets.py")

        caplog.set_level(logging.ERROR, logger="app.errors")

        response = error_client.get(add_route(app, bug))

        body = assert_problem(response, 500, "internal_error")
        assert body["violations"] == []
        for leak in ("hunter2", "RuntimeError", "secrets.py", "Traceback"):
            assert leak not in response.text
        [record] = [r for r in caplog.records if r.name == "app.errors"]
        assert body["request_id"] in record.getMessage()
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1], RuntimeError)

    def test_an_unexpected_database_error_is_a_generic_500_without_the_sql(
        self,
        app: FastAPI,
        error_client: TestClient,
        make_medicine: MedicineFactory,
    ) -> None:
        medicine = make_medicine()

        def invalid_period(session: Session) -> None:
            # Violates ck_formulary_rules_period_ordered: an application bug, not a client error.
            session.execute(
                text(
                    "INSERT INTO formulary_rules (medicine_id, effective_from, effective_to, "
                    "max_quantity_per_dispense, max_quantity_per_30_days) "
                    "VALUES (:id, '2026-02-01', '2026-01-01', 1, 1)"
                ),
                {"id": medicine.id},
            )

        response = error_client.get(add_sql_route(app, "invalid_period", invalid_period))

        assert_problem(response, 500, "internal_error")
        assert "formulary_rules" not in response.text

    def test_a_nul_character_that_slipped_past_validation_is_a_500_not_a_hang(
        self, app: FastAPI, error_client: TestClient
    ) -> None:
        def nul(session: Session) -> None:
            session.execute(text("SELECT :value"), {"value": "a\x00b"})

        assert_problem(error_client.get(add_sql_route(app, "nul", nul)), 500, "internal_error")


class TestDatabaseErrors:
    def test_an_overlapping_rule_period_is_a_409_rule_conflict_and_leaves_nothing_behind(
        self,
        app: FastAPI,
        client: TestClient,
        db: Session,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
    ) -> None:
        medicine = make_medicine()
        make_rule(medicine, date(2026, 1, 1))

        def overlapping(session: Session) -> None:
            session.execute(
                text(
                    "INSERT INTO formulary_rules (medicine_id, effective_from, effective_to, "
                    "max_quantity_per_dispense, max_quantity_per_30_days) "
                    "VALUES (:id, '2026-06-01', NULL, 1, 1)"
                ),
                {"id": medicine.id},
            )

        body = assert_problem(
            client.get(add_sql_route(app, "overlapping", overlapping)), 409, "rule_conflict"
        )

        assert violation_pairs(body) == [("rule_period_overlap", "effective_from")]
        assert db.scalar(select(func.count()).select_from(FormularyRule)) == 1

    def test_a_lock_timeout_is_a_503_resource_busy(
        self,
        app: FastAPI,
        client: TestClient,
        engine: Engine,
        make_medicine: MedicineFactory,
    ) -> None:
        medicine = make_medicine()

        def locked(session: Session) -> None:
            session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            session.execute(
                text("SELECT id FROM medicines WHERE id = :id FOR UPDATE"), {"id": medicine.id}
            )

        path = add_sql_route(app, "locked", locked)
        with engine.connect() as holder:
            holder.execute(
                text("SELECT id FROM medicines WHERE id = :id FOR UPDATE"), {"id": medicine.id}
            )
            response = client.get(path)

        assert_problem(response, 503, "resource_busy")
        assert response.headers["retry-after"] == "1"

    def test_a_statement_timeout_is_a_503_service_unavailable(
        self, app: FastAPI, client: TestClient
    ) -> None:
        def slow(session: Session) -> None:
            session.execute(text("SET LOCAL statement_timeout = '50ms'"))
            session.execute(text("SELECT pg_sleep(10)"))

        body = assert_problem(
            client.get(add_sql_route(app, "slow", slow)), 503, "service_unavailable"
        )

        assert "too long" in body["detail"]

    def test_an_exhausted_connection_pool_is_a_503_service_unavailable(
        self, database_url: str, settings: Settings, clock: FakeClock
    ) -> None:
        engine = create_engine(database_url, pool_size=1, max_overflow=0, pool_timeout=0.2)
        client = TestClient(create_app(engine=engine, settings=settings, clock=clock))

        with engine.connect():
            response = client.get("/healthz")

        assert_problem(response, 503, "service_unavailable")
        engine.dispose()


class TestValidationViolations:
    """Pydantic errors, translated into stable violation codes; never echoing the input."""

    @pytest.mark.parametrize(
        ("error", "code", "field"),
        [
            ({"type": "missing", "loc": ("body", "name")}, "required", "name"),
            ({"type": "missing", "loc": ("body",)}, "required", None),
            ({"type": "extra_forbidden", "loc": ("body", "colour")}, "unknown_field", "colour"),
            ({"type": "string_too_short", "loc": ("body", "a")}, "too_short", "a"),
            ({"type": "too_short", "loc": ("body", "a")}, "too_short", "a"),
            ({"type": "string_too_long", "loc": ("body", "a")}, "too_long", "a"),
            ({"type": "too_long", "loc": ("body", "a")}, "too_long", "a"),
            ({"type": "greater_than", "loc": ("query", "limit")}, "out_of_range", "limit"),
            ({"type": "greater_than_equal", "loc": ("query", "limit")}, "out_of_range", "limit"),
            ({"type": "less_than", "loc": ("query", "limit")}, "out_of_range", "limit"),
            ({"type": "less_than_equal", "loc": ("query", "limit")}, "out_of_range", "limit"),
            ({"type": "int_parsing_size", "loc": ("query", "limit")}, "out_of_range", "limit"),
            ({"type": "string_type", "loc": ("body", "a")}, "invalid_type", "a"),
            ({"type": "int_type", "loc": ("body", "a")}, "invalid_type", "a"),
            ({"type": "bool_type", "loc": ("body", "a")}, "invalid_type", "a"),
            ({"type": "model_attributes_type", "loc": ("body",)}, "invalid_type", None),
            ({"type": "int_parsing", "loc": ("query", "limit")}, "invalid_format", "limit"),
            ({"type": "date_parsing", "loc": ("body", "on")}, "invalid_format", "on"),
            ({"type": "datetime_parsing", "loc": ("body", "at")}, "invalid_format", "at"),
            ({"type": "string_pattern_mismatch", "loc": ("body", "a")}, "invalid_format", "a"),
            ({"type": "string_unicode", "loc": ("body", "a")}, "invalid_format", "a"),
            ({"type": "value_error", "loc": ("body", "a")}, "invalid_value", "a"),
            ({"type": "assertion_error", "loc": ("body", "a")}, "invalid_value", "a"),
            ({"type": "literal_error", "loc": ("query", "mode")}, "invalid_value", "mode"),
            ({"type": "never_heard_of_it", "loc": ("body", "a")}, "invalid_value", "a"),
            # Codes our own validators raise are passed through.
            ({"type": "invalid_format", "loc": ("body", "a")}, "invalid_format", "a"),
            ({"type": "out_of_range", "loc": ("body", "a")}, "out_of_range", "a"),
            ({"type": "invalid_type", "loc": ("body", "a")}, "invalid_type", "a"),
        ],
    )
    def test_maps_the_error_type_to_a_stable_code_and_the_location_to_a_field(
        self, error: dict[str, Any], code: str, field: str | None
    ) -> None:
        violation = errors.violation_from_validation_error({"msg": "message", **error})

        assert (violation.code, violation.field) == (code, field)

    @pytest.mark.parametrize(
        ("loc", "field"),
        [
            (("body", "items", 0, "quantity"), "items.0.quantity"),
            (("query", "cursor"), "cursor"),
            (("path", "code"), "code"),
            (("header", "x-token"), "x-token"),
            (("body", "body"), "body"),
            (("body",), None),
        ],
    )
    def test_joins_the_location_below_the_parameter_source(
        self, loc: tuple[object, ...], field: str | None
    ) -> None:
        violation = errors.violation_from_validation_error(
            {"type": "missing", "loc": loc, "msg": "m"}
        )

        assert violation.field == field

    def test_a_syntax_error_in_the_body_has_no_field_and_reports_where(self) -> None:
        violation = errors.violation_from_validation_error(
            {
                "type": "json_invalid",
                "loc": ("body", 7),
                "msg": "JSON decode error",
                "ctx": {"error": "Expecting value"},
            }
        )

        assert (violation.code, violation.field) == ("invalid_json", None)
        assert violation.context == {"position": 7}
        assert "Expecting value" in violation.message

    def test_only_constraints_of_the_schema_reach_the_context(self) -> None:
        violation = errors.violation_from_validation_error(
            {
                "type": "string_too_long",
                "loc": ("body", "a"),
                "msg": "String should have at most 5 characters",
                "ctx": {"max_length": 5, "given": "secret", "error": ValueError("secret")},
                "input": "secret",
            }
        )

        assert violation.context == {"max_length": 5}

    @pytest.mark.parametrize("error_type", ["value_error", "assertion_error", "never_heard_of_it"])
    def test_the_message_of_an_unrecognised_error_is_never_passed_on(self, error_type: str) -> None:
        violation = errors.violation_from_validation_error(
            {
                "type": error_type,
                "loc": ("body", "a"),
                "msg": "Value error, the secret is hunter2",
                "ctx": {"error": ValueError("hunter2")},
                "input": "hunter2",
            }
        )

        assert "hunter2" not in repr(violation)
