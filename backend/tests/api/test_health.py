"""GET /healthz: 200 while the database answers, a problem+json 503 when it does not."""

import psycopg
from fastapi import FastAPI
from sqlalchemy import create_engine
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.api.support import FakeClock, assert_problem


def test_reports_ok_while_the_database_answers(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_is_not_part_of_the_versioned_api(client: TestClient) -> None:
    assert_problem(client.get("/api/v1/healthz"), 404, "not_found")


def test_answers_503_service_unavailable_when_the_database_cannot_be_reached(
    settings: Settings, clock: FakeClock
) -> None:
    def refuse_connection() -> psycopg.Connection:
        raise psycopg.OperationalError("connection refused")

    # The creator fails the way a dead database does, without a slow real connection attempt.
    engine = create_engine("postgresql+psycopg://", creator=refuse_connection)
    app: FastAPI = create_app(engine=engine, settings=settings, clock=clock)

    response = TestClient(app).get("/healthz")

    body = assert_problem(response, 503, "service_unavailable")
    assert body["violations"] == []
