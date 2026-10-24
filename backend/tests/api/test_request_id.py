"""Every response carries X-Request-ID, and an error body repeats it as request_id."""

import re

import pytest
from starlette.testclient import TestClient

from app.api.request_id import REQUEST_ID_HEADER

GENERATED = re.compile(r"[0-9a-f]{32}")


def test_a_response_without_a_client_id_gets_a_generated_one(client: TestClient) -> None:
    response = client.get("/healthz")

    assert GENERATED.fullmatch(response.headers[REQUEST_ID_HEADER])


def test_every_request_gets_its_own_id(client: TestClient) -> None:
    ids = {client.get("/healthz").headers[REQUEST_ID_HEADER] for _ in range(5)}

    assert len(ids) == 5


@pytest.mark.parametrize(
    "supplied",
    ["abcdefgh", "a" * 64, "9f0c1c5e-7b1d-4a52-8f55-0c2d1e3f4a5b", "trace.id_01-A"],
)
def test_a_sane_client_id_is_reused(client: TestClient, supplied: str) -> None:
    response = client.get("/healthz", headers={REQUEST_ID_HEADER: supplied})

    assert response.headers[REQUEST_ID_HEADER] == supplied


@pytest.mark.parametrize(
    "supplied",
    [
        "short",
        "a" * 65,
        "has space inside",
        "semi;colon",
        "slash/slash",
        'quote"quote',
        "cafécafe".encode("latin-1"),
    ],
)
def test_an_unsuitable_client_id_is_replaced(client: TestClient, supplied: str | bytes) -> None:
    response = client.get("/healthz", headers={REQUEST_ID_HEADER: supplied})

    assert GENERATED.fullmatch(response.headers[REQUEST_ID_HEADER])


def test_the_id_is_on_errors_and_matches_the_body(client: TestClient) -> None:
    response = client.get(
        "/api/v1/medicines/NOPE", headers={REQUEST_ID_HEADER: "client-trace-0001"}
    )

    assert response.status_code == 404
    assert response.headers[REQUEST_ID_HEADER] == "client-trace-0001"
    assert response.json()["request_id"] == "client-trace-0001"


def test_the_id_is_on_unhandled_errors_too(
    error_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.medicines.search_medicines", explode)

    response = error_client.get(
        "/api/v1/medicines", headers={REQUEST_ID_HEADER: "client-trace-0002"}
    )

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == "client-trace-0002"
    assert response.json()["request_id"] == "client-trace-0002"
