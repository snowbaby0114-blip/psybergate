"""Conformance: every error path of the API answers in exactly the shape of the error contract.

Each error path is a registered case: a trigger that provokes it, the status and code it must
answer with, and the (code, field) of every violation it must list. One parametrised test then
checks the whole contract on all of them, so a new endpoint's error paths join the check by
registering a case below (see `error_case`); nothing else needs to change.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

import psycopg
import pytest
from httpx import Response
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from app import repositories
from app.config import Settings
from app.domain.rules import SupersedePlan
from app.main import create_app
from app.models import Dispense, FormularyRule, Medicine
from app.pagination import encode_cursor
from app.services import formulary
from tests.api.dispense_support import DISPENSES, OMIT, hold_advisory_lock, post_dispense
from tests.api.support import FakeClock, assert_problem, violation_pairs

RULES = "/api/v1/medicines/{code}/rules"
NEW_RULE = {
    "effective_from": "2026-09-01",
    "max_quantity_per_dispense": 30,
    "max_quantity_per_30_days": 60,
}


@dataclass(frozen=True)
class Scenario:
    """What a trigger needs: a client that answers bugs with a 500 instead of raising, and the same
    factories and fixtures the other API tests use."""

    client: TestClient
    engine: Engine
    settings: Settings
    clock: FakeClock
    monkeypatch: pytest.MonkeyPatch
    make_medicine: Callable[..., Medicine]
    make_rule: Callable[..., FormularyRule]
    make_dispense: Callable[..., Dispense]

    @contextmanager
    def dispense_in_flight(self, medicine: Medicine) -> Iterator[None]:
        """Hold the medicine row FOR SHARE, as a dispense does until it commits."""
        with self.engine.connect() as holder:
            holder.execute(
                text("SELECT id FROM medicines WHERE id = :id FOR SHARE"), {"id": medicine.id}
            )
            yield


@dataclass(frozen=True)
class ErrorCase:
    trigger: Callable[[Scenario], Response]
    status: int
    code: str
    violations: tuple[tuple[str, str | None], ...] = ()
    extra_keys: frozenset[str] = frozenset()


CASES: dict[str, ErrorCase] = {}


def error_case(
    name: str,
    status: int,
    code: str,
    *,
    violations: tuple[tuple[str, str | None], ...] = (),
    extra_keys: frozenset[str] = frozenset(),
) -> Callable[[Callable[[Scenario], Response]], Callable[[Scenario], Response]]:
    """Register the decorated trigger as an error path under `name`."""

    def register(trigger: Callable[[Scenario], Response]) -> Callable[[Scenario], Response]:
        assert name not in CASES, f"two error cases are called {name}"
        CASES[name] = ErrorCase(trigger, status, code, violations, extra_keys)
        return trigger

    return register


@pytest.fixture
def scenario(
    error_client: TestClient,
    engine: Engine,
    settings: Settings,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    make_medicine: Callable[..., Medicine],
    make_rule: Callable[..., FormularyRule],
    make_dispense: Callable[..., Dispense],
) -> Scenario:
    return Scenario(
        client=error_client,
        engine=engine,
        settings=settings,
        clock=clock,
        monkeypatch=monkeypatch,
        make_medicine=make_medicine,
        make_rule=make_rule,
        make_dispense=make_dispense,
    )


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_every_error_path_answers_in_the_contract_shape(
    case: ErrorCase, scenario: Scenario
) -> None:
    response = case.trigger(scenario)

    body = assert_problem(response, case.status, case.code, extra_keys=case.extra_keys)

    assert tuple(violation_pairs(body)) == case.violations


# --- routing -----------------------------------------------------------------------------------


@error_case("unknown_path", 404, "not_found")
def unknown_path(s: Scenario) -> Response:
    return s.client.get("/api/v1/nowhere")


@error_case("trailing_slash", 404, "not_found")
def trailing_slash(s: Scenario) -> Response:
    return s.client.get("/api/v1/medicines/")


@error_case("wrong_method", 405, "method_not_allowed")
def wrong_method(s: Scenario) -> Response:
    return s.client.delete("/api/v1/medicines")


# --- GET /medicines ----------------------------------------------------------------------------


@error_case("limit_above_100", 422, "validation_error", violations=(("out_of_range", "limit"),))
def limit_above_100(s: Scenario) -> Response:
    return s.client.get("/api/v1/medicines", params={"limit": 101})


@error_case("limit_zero", 422, "validation_error", violations=(("out_of_range", "limit"),))
def limit_zero(s: Scenario) -> Response:
    return s.client.get("/api/v1/medicines", params={"limit": 0})


@error_case(
    "limit_not_a_number", 422, "validation_error", violations=(("invalid_format", "limit"),)
)
def limit_not_a_number(s: Scenario) -> Response:
    return s.client.get("/api/v1/medicines", params={"limit": "many"})


@error_case("search_term_too_long", 422, "validation_error", violations=(("too_long", "q"),))
def search_term_too_long(s: Scenario) -> Response:
    return s.client.get("/api/v1/medicines", params={"q": "x" * 101})


@error_case("search_term_with_nul", 422, "validation_error", violations=(("invalid_format", "q"),))
def search_term_with_nul(s: Scenario) -> Response:
    return s.client.get("/api/v1/medicines", params={"q": "a\x00b"})


@error_case("malformed_cursor", 400, "invalid_cursor", violations=(("invalid_cursor", "cursor"),))
def malformed_cursor(s: Scenario) -> Response:
    return s.client.get("/api/v1/medicines", params={"cursor": "garbage"})


@error_case("forged_cursor", 400, "invalid_cursor", violations=(("invalid_cursor", "cursor"),))
def forged_cursor(s: Scenario) -> Response:
    return s.client.get(
        "/api/v1/medicines", params={"cursor": encode_cursor(["a\x00", "b"], {"q": None})}
    )


@error_case(
    "cursor_for_another_search", 400, "invalid_cursor", violations=(("invalid_cursor", "cursor"),)
)
def cursor_for_another_search(s: Scenario) -> Response:
    cursor = encode_cursor(["a", "b"], {"q": "other"})
    return s.client.get("/api/v1/medicines", params={"cursor": cursor})


# --- medicines by code -------------------------------------------------------------------------


@error_case("medicine_not_found", 404, "medicine_not_found")
def medicine_not_found(s: Scenario) -> Response:
    return s.client.get("/api/v1/medicines/NOPE")


@error_case("rules_of_unknown_medicine", 404, "medicine_not_found")
def rules_of_unknown_medicine(s: Scenario) -> Response:
    return s.client.get(RULES.format(code="NOPE"))


@error_case("supersede_unknown_medicine", 404, "medicine_not_found")
def supersede_unknown_medicine(s: Scenario) -> Response:
    return s.client.post(RULES.format(code="NOPE"), json=NEW_RULE)


@error_case(
    "code_with_control_character", 422, "validation_error", violations=(("invalid_format", "code"),)
)
def code_with_control_character(s: Scenario) -> Response:
    return s.client.get("/api/v1/medicines/a%00b")


@error_case("code_too_long", 422, "validation_error", violations=(("too_long", "code"),))
def code_too_long(s: Scenario) -> Response:
    return s.client.get(f"/api/v1/medicines/{'x' * 65}/rules")


# --- POST /medicines/{code}/rules --------------------------------------------------------------


@error_case("supersede_without_body", 422, "validation_error", violations=(("required", None),))
def supersede_without_body(s: Scenario) -> Response:
    s.make_medicine(code="MED-0001")
    return s.client.post(RULES.format(code="MED-0001"))


@error_case(
    "supersede_malformed_json", 422, "validation_error", violations=(("invalid_json", None),)
)
def supersede_malformed_json(s: Scenario) -> Response:
    s.make_medicine(code="MED-0001")
    return s.client.post(
        RULES.format(code="MED-0001"), content=b"{", headers={"content-type": "application/json"}
    )


@error_case(
    "supersede_body_not_an_object", 422, "validation_error", violations=(("invalid_type", None),)
)
def supersede_body_not_an_object(s: Scenario) -> Response:
    s.make_medicine(code="MED-0001")
    return s.client.post(RULES.format(code="MED-0001"), json=[1, 2])


@error_case(
    "supersede_fields_missing",
    422,
    "validation_error",
    violations=(
        ("required", "effective_from"),
        ("required", "max_quantity_per_dispense"),
        ("required", "max_quantity_per_30_days"),
    ),
)
def supersede_fields_missing(s: Scenario) -> Response:
    s.make_medicine(code="MED-0001")
    return s.client.post(RULES.format(code="MED-0001"), json={})


@error_case(
    "supersede_unknown_field", 422, "validation_error", violations=(("unknown_field", "colour"),)
)
def supersede_unknown_field(s: Scenario) -> Response:
    s.make_medicine(code="MED-0001")
    return s.client.post(RULES.format(code="MED-0001"), json=NEW_RULE | {"colour": "red"})


@error_case(
    "supersede_limits_out_of_range",
    422,
    "validation_error",
    violations=(
        ("out_of_range", "max_quantity_per_dispense"),
        ("out_of_range", "max_quantity_per_30_days"),
    ),
)
def supersede_limits_out_of_range(s: Scenario) -> Response:
    s.make_medicine(code="MED-0001")
    body = NEW_RULE | {"max_quantity_per_dispense": 0, "max_quantity_per_30_days": 2**31}
    return s.client.post(RULES.format(code="MED-0001"), json=body)


@error_case(
    "supersede_date_not_a_date",
    422,
    "validation_error",
    violations=(("invalid_format", "effective_from"),),
)
def supersede_date_not_a_date(s: Scenario) -> Response:
    s.make_medicine(code="MED-0001")
    return s.client.post(
        RULES.format(code="MED-0001"), json=NEW_RULE | {"effective_from": "2026-02-30"}
    )


@error_case(
    "supersede_date_out_of_range",
    422,
    "validation_error",
    violations=(("out_of_range", "effective_from"),),
)
def supersede_date_out_of_range(s: Scenario) -> Response:
    s.make_medicine(code="MED-0001")
    return s.client.post(
        RULES.format(code="MED-0001"), json=NEW_RULE | {"effective_from": "2101-01-01"}
    )


@error_case(
    "rule_already_starts_on_date",
    409,
    "rule_conflict",
    violations=(("rule_already_starts_on_date", "effective_from"),),
)
def rule_already_starts_on_date(s: Scenario) -> Response:
    medicine = s.make_medicine(code="MED-0001")
    s.make_rule(medicine, date(2026, 9, 1))
    return s.client.post(RULES.format(code="MED-0001"), json=NEW_RULE)


@error_case(
    "rule_would_alter_recorded_dispenses",
    409,
    "rule_conflict",
    violations=(("rule_would_alter_recorded_dispenses", "effective_from"),),
)
def rule_would_alter_recorded_dispenses(s: Scenario) -> Response:
    medicine = s.make_medicine(code="MED-0001")
    rule = s.make_rule(medicine, date(2026, 1, 1))
    s.make_dispense(medicine, rule)  # on 15 March 2026, inside what the new rule would govern
    return s.client.post(
        RULES.format(code="MED-0001"), json=NEW_RULE | {"effective_from": "2026-03-01"}
    )


@error_case(
    "rule_period_overlap",
    409,
    "rule_conflict",
    violations=(("rule_period_overlap", "effective_from"),),
)
def rule_period_overlap(s: Scenario) -> Response:
    medicine = s.make_medicine(code="MED-0001")
    first = s.make_rule(medicine, date(2026, 1, 1), date(2026, 6, 1))
    s.make_rule(medicine, date(2026, 6, 1))
    s.monkeypatch.setattr(
        formulary,
        "plan_supersede",
        lambda rules, effective_from: SupersedePlan(
            truncate_rule_id=first.id, new_effective_to=None
        ),
    )
    return s.client.post(
        RULES.format(code="MED-0001"), json=NEW_RULE | {"effective_from": "2026-03-01"}
    )


# --- POST /dispenses ---------------------------------------------------------------------------


def medicine_with_a_rule(s: Scenario) -> Medicine:
    """MED-0001 with a rule in force since 1 January 2026: 10 per dispense, 30 per 30 days."""
    medicine = s.make_medicine(code="MED-0001")
    s.make_rule(medicine, date(2026, 1, 1))
    return medicine


@error_case("dispense_without_body", 422, "validation_error", violations=(("required", None),))
def dispense_without_body(s: Scenario) -> Response:
    return s.client.post(DISPENSES)


@error_case(
    "dispense_malformed_json", 422, "validation_error", violations=(("invalid_json", None),)
)
def dispense_malformed_json(s: Scenario) -> Response:
    return s.client.post(DISPENSES, content=b"{", headers={"content-type": "application/json"})


@error_case(
    "dispense_fields_missing",
    422,
    "validation_error",
    violations=(
        ("required", "idempotency_key"),
        ("required", "medicine_code"),
        ("required", "patient_ref"),
        ("required", "quantity"),
    ),
)
def dispense_fields_missing(s: Scenario) -> Response:
    return s.client.post(DISPENSES, json={})


@error_case(
    "dispense_unknown_field", 422, "validation_error", violations=(("unknown_field", "medicine"),)
)
def dispense_unknown_field(s: Scenario) -> Response:
    return post_dispense(s.client, medicine="MED-0001")


@error_case(
    "dispense_timestamp_without_offset",
    422,
    "validation_error",
    violations=(("invalid_format", "dispensed_at"),),
)
def dispense_timestamp_without_offset(s: Scenario) -> Response:
    return post_dispense(s.client, dispensed_at="2026-03-15T10:00:00")


@error_case(
    "dispense_timestamp_out_of_range",
    422,
    "validation_error",
    violations=(("out_of_range", "dispensed_at"),),
)
def dispense_timestamp_out_of_range(s: Scenario) -> Response:
    return post_dispense(s.client, dispensed_at="9999-12-31T23:59:59Z")


@error_case(
    "dispense_quantity_out_of_range",
    422,
    "validation_error",
    violations=(("out_of_range", "quantity"),),
)
def dispense_quantity_out_of_range(s: Scenario) -> Response:
    return post_dispense(s.client, quantity=0)


@error_case(
    "dispense_control_character",
    422,
    "validation_error",
    violations=(("invalid_format", "patient_ref"),),
)
def dispense_control_character(s: Scenario) -> Response:
    return post_dispense(s.client, patient_ref="a\x00b")


@error_case(
    "dispense_idempotency_key_with_a_space",
    422,
    "validation_error",
    violations=(("invalid_format", "idempotency_key"),),
)
def dispense_idempotency_key_with_a_space(s: Scenario) -> Response:
    return post_dispense(s.client, idempotency_key="not a key")


@error_case(
    "dispense_rejected_by_several_rules",
    422,
    "dispense_rejected",
    violations=(
        ("max_quantity_per_dispense_exceeded", "quantity"),
        ("max_quantity_per_30_days_exceeded", "quantity"),
    ),
    extra_keys=frozenset({"attempt_id"}),
)
def dispense_rejected_by_several_rules(s: Scenario) -> Response:
    medicine_with_a_rule(s)
    return post_dispense(s.client, quantity=40)


@error_case(
    "dispense_of_an_unknown_medicine",
    422,
    "dispense_rejected",
    violations=(("medicine_not_found", "medicine_code"),),
    extra_keys=frozenset({"attempt_id"}),
)
def dispense_of_an_unknown_medicine(s: Scenario) -> Response:
    return post_dispense(s.client, medicine_code="NOPE")


@error_case(
    "dispense_of_an_inactive_medicine_without_a_rule",
    422,
    "dispense_rejected",
    violations=(("medicine_inactive", "medicine_code"), ("no_rule_in_force", "dispensed_at")),
    extra_keys=frozenset({"attempt_id"}),
)
def dispense_of_an_inactive_medicine_without_a_rule(s: Scenario) -> Response:
    s.make_medicine(code="MED-0001", is_active=False)
    return post_dispense(s.client)


@error_case(
    "dispense_rejection_replayed",
    422,
    "dispense_rejected",
    violations=(
        ("max_quantity_per_dispense_exceeded", "quantity"),
        ("max_quantity_per_30_days_exceeded", "quantity"),
    ),
    extra_keys=frozenset({"attempt_id"}),
)
def dispense_rejection_replayed(s: Scenario) -> Response:
    medicine_with_a_rule(s)
    post_dispense(s.client, quantity=40)
    return post_dispense(s.client, quantity=40)


@error_case(
    "dispense_idempotency_key_reused",
    422,
    "idempotency_key_reuse",
    violations=(("idempotency_key_reuse", "idempotency_key"),),
)
def dispense_idempotency_key_reused(s: Scenario) -> Response:
    medicine_with_a_rule(s)
    post_dispense(s.client)
    return post_dispense(s.client, quantity=6)


@error_case(
    "dispense_omitted_instant_against_explicit_one",
    422,
    "idempotency_key_reuse",
    violations=(("idempotency_key_reuse", "idempotency_key"),),
)
def dispense_omitted_instant_against_explicit_one(s: Scenario) -> Response:
    medicine_with_a_rule(s)
    post_dispense(s.client, dispensed_at=OMIT)
    return post_dispense(s.client, dispensed_at="2026-06-15T10:00:00Z")


# --- GET /dispenses ----------------------------------------------------------------------------

UNFILTERED = {"patient_ref": None, "medicine": None}


@error_case(
    "dispenses_malformed_cursor", 400, "invalid_cursor", violations=(("invalid_cursor", "cursor"),)
)
def dispenses_malformed_cursor(s: Scenario) -> Response:
    return s.client.get(DISPENSES, params={"cursor": "garbage"})


@error_case(
    "dispenses_forged_cursor", 400, "invalid_cursor", violations=(("invalid_cursor", "cursor"),)
)
def dispenses_forged_cursor(s: Scenario) -> Response:
    cursor = encode_cursor(["2026-03-02T10:00:00+02:00", 1], UNFILTERED)
    return s.client.get(DISPENSES, params={"cursor": cursor})


@error_case(
    "dispenses_cursor_for_other_filters",
    400,
    "invalid_cursor",
    violations=(("invalid_cursor", "cursor"),),
)
def dispenses_cursor_for_other_filters(s: Scenario) -> Response:
    cursor = encode_cursor(
        ["2026-03-02T10:00:00.000000+00:00", 1], {"patient_ref": "PT-1", "medicine": None}
    )
    return s.client.get(DISPENSES, params={"patient_ref": "PT-2", "cursor": cursor})


@error_case(
    "dispenses_limit_above_100", 422, "validation_error", violations=(("out_of_range", "limit"),)
)
def dispenses_limit_above_100(s: Scenario) -> Response:
    return s.client.get(DISPENSES, params={"limit": 101})


@error_case(
    "dispenses_blank_patient_filter",
    422,
    "validation_error",
    violations=(("too_short", "patient_ref"),),
)
def dispenses_blank_patient_filter(s: Scenario) -> Response:
    return s.client.get(DISPENSES, params={"patient_ref": " "})


@error_case(
    "dispenses_medicine_filter_with_a_control_character",
    422,
    "validation_error",
    violations=(("invalid_format", "medicine"),),
)
def dispenses_medicine_filter_with_a_control_character(s: Scenario) -> Response:
    return s.client.get(DISPENSES, params={"medicine": "a\x00b"})


# --- infrastructure ----------------------------------------------------------------------------


@error_case("resource_busy", 503, "resource_busy")
def resource_busy(s: Scenario) -> Response:
    medicine = s.make_medicine(code="MED-0001")
    s.settings.lock_timeout_ms = 100
    with s.dispense_in_flight(medicine):
        return s.client.post(RULES.format(code="MED-0001"), json=NEW_RULE)


@error_case("database_unreachable", 503, "service_unavailable")
def database_unreachable(s: Scenario) -> Response:
    def refuse_connection() -> psycopg.Connection:
        raise psycopg.OperationalError("connection refused")

    engine = create_engine("postgresql+psycopg://", creator=refuse_connection)
    app = create_app(engine=engine, settings=s.settings, clock=s.clock)
    return TestClient(app, raise_server_exceptions=False).get("/healthz")


@error_case("unhandled_exception", 500, "internal_error")
def unhandled_exception(s: Scenario) -> Response:
    def bug(*args: object, **kwargs: object) -> None:
        raise RuntimeError("a bug")

    s.monkeypatch.setattr("app.services.medicines.search_medicines", bug)
    return s.client.get("/api/v1/medicines")


@error_case("dispense_resource_busy", 503, "resource_busy")
def dispense_resource_busy(s: Scenario) -> Response:
    medicine_with_a_rule(s)
    s.settings.lock_timeout_ms = 100
    with hold_advisory_lock(sessionmaker(s.engine), "idem", "key-1"):
        return post_dispense(s.client)


@error_case("dispense_deadlock", 503, "resource_busy")
def dispense_deadlock(s: Scenario) -> Response:
    medicine_with_a_rule(s)

    def deadlocked(*args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, psycopg.errors.DeadlockDetected("deadlock detected"))

    s.monkeypatch.setattr(repositories, "pair_history", deadlocked)
    return post_dispense(s.client)


@error_case("dispense_unhandled_exception", 500, "internal_error")
def dispense_unhandled_exception(s: Scenario) -> Response:
    medicine_with_a_rule(s)

    def bug(*args: object, **kwargs: object) -> None:
        raise RuntimeError("a bug")

    s.monkeypatch.setattr(repositories, "insert_attempt", bug)
    return post_dispense(s.client)
