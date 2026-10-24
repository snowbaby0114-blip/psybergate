"""create_app: start-up and shutdown of the engine, and the published OpenAPI document."""

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.api.support import FakeClock, assert_problem

BACKEND = Path(__file__).resolve().parents[2]
# Nothing listens on this port: any attempt to connect fails after seconds, not instantly.
UNREACHABLE_DATABASE = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"


@pytest.fixture
def app_logger() -> Iterator[logging.Logger]:
    """The lifespan configures the application logger; put it back as the test found it."""
    logger = logging.getLogger("app")
    handlers, level = list(logger.handlers), logger.level
    yield logger
    logger.handlers[:] = handlers
    logger.setLevel(level)


class TestEngineLifecycle:
    def test_creating_the_app_does_not_touch_the_database(self) -> None:
        app = create_app(settings=Settings(database_url=UNREACHABLE_DATABASE))

        assert not hasattr(app.state, "engine")

    def test_starting_the_app_does_not_connect_either(self, app_logger: logging.Logger) -> None:
        app = create_app(settings=Settings(database_url=UNREACHABLE_DATABASE))

        with TestClient(app):
            assert app.state.engine is not None

    def test_importing_the_module_does_not_connect(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=BACKEND,
            env={**os.environ, "DATABASE_URL": UNREACHABLE_DATABASE},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert result.returncode == 0, result.stderr

    def test_shutdown_disposes_the_engine_the_app_created(
        self, settings: Settings, app_logger: logging.Logger
    ) -> None:
        app = create_app(settings=settings)

        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert app.state.engine.pool.checkedin() == 1

        assert app.state.engine.pool.checkedin() == 0

    def test_an_engine_passed_in_is_left_to_its_owner(
        self, engine: Engine, settings: Settings, clock: FakeClock, app_logger: logging.Logger
    ) -> None:
        app = create_app(engine=engine, settings=settings, clock=clock)
        pool = engine.pool

        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200

        assert engine.pool is pool
        assert app.state.engine is engine

    def test_the_log_level_setting_applies_to_the_application_loggers(
        self, settings: Settings, app_logger: logging.Logger
    ) -> None:
        settings.log_level = "ERROR"

        with TestClient(create_app(settings=settings)):
            assert logging.getLogger("app.errors").getEffectiveLevel() == logging.ERROR
            assert logging.getLogger("sqlalchemy.engine").getEffectiveLevel() != logging.ERROR


class TestOpenApi:
    @pytest.fixture
    def openapi(self, client: TestClient) -> dict[str, Any]:
        response = client.get("/api/openapi.json")
        assert response.status_code == 200
        return response.json()

    def test_docs_and_schema_live_under_api(self, client: TestClient) -> None:
        assert client.get("/api/docs").status_code == 200
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert_problem(client.get(path), 404, "not_found")

    def test_documents_every_endpoint(self, openapi: dict[str, Any]) -> None:
        documented = {
            (method.upper(), path) for path, item in openapi["paths"].items() for method in item
        }

        assert documented >= {
            ("GET", "/healthz"),
            ("GET", "/api/v1/medicines"),
            ("GET", "/api/v1/medicines/{code}"),
            ("GET", "/api/v1/medicines/{code}/rules"),
            ("POST", "/api/v1/medicines/{code}/rules"),
        }

    def test_every_error_response_is_a_problem_document(self, openapi: dict[str, Any]) -> None:
        errors = [
            (path, method, status, response)
            for path, item in openapi["paths"].items()
            for method, operation in item.items()
            for status, response in operation["responses"].items()
            if int(status) >= 400
        ]

        assert errors
        for path, method, status, response in errors:
            assert response["content"] == {
                "application/problem+json": {"schema": {"$ref": "#/components/schemas/ProblemOut"}}
            }, f"{method} {path} {status}"

    def test_fastapis_own_validation_error_schema_is_not_published(
        self, openapi: dict[str, Any]
    ) -> None:
        schemas = openapi["components"]["schemas"]

        assert "HTTPValidationError" not in schemas
        assert "ValidationError" not in schemas

    def test_validation_failures_are_documented_on_every_operation_with_input(
        self, openapi: dict[str, Any]
    ) -> None:
        for path, item in openapi["paths"].items():
            for method, operation in item.items():
                takes_input = bool(operation.get("parameters") or operation.get("requestBody"))
                assert ("422" in operation["responses"]) == takes_input, f"{method} {path}"

    def test_the_problem_schema_describes_the_error_body(self, openapi: dict[str, Any]) -> None:
        problem = openapi["components"]["schemas"]["ProblemOut"]

        assert set(problem["properties"]) == {
            "type",
            "title",
            "status",
            "detail",
            "code",
            "request_id",
            "violations",
            "attempt_id",
        }
        assert set(problem["required"]) == {
            "type",
            "title",
            "status",
            "detail",
            "code",
            "request_id",
            "violations",
        }
