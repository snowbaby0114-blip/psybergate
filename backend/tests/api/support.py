"""Helpers shared by the API test modules."""

from datetime import datetime
from typing import Any

from httpx import Response


class FakeClock:
    """The application's clock in tests: set `now` (an aware datetime) to move time."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


PROBLEM_KEYS = {"type", "title", "status", "detail", "code", "request_id", "violations"}
VIOLATION_KEYS = {"code", "field", "message", "context"}


def assert_problem(
    response: Response, status: int, code: str, *, extra_keys: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Assert the whole error contract on a response and return its body.

    `extra_keys` names the members a specific problem adds, such as attempt_id.
    """
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert set(body) == PROBLEM_KEYS | extra_keys
    assert body["type"] == f"urn:emguidance:problem:{code}"
    assert body["status"] == status
    assert body["code"] == code
    assert isinstance(body["title"], str) and body["title"]
    assert isinstance(body["detail"], str) and body["detail"]
    assert body["request_id"] == response.headers["x-request-id"]
    assert isinstance(body["violations"], list)
    for violation in body["violations"]:
        assert set(violation) == VIOLATION_KEYS
        assert isinstance(violation["code"], str) and violation["code"]
        assert violation["field"] is None or isinstance(violation["field"], str)
        assert isinstance(violation["message"], str) and violation["message"]
        assert isinstance(violation["context"], dict)
    return body


def violation_pairs(body: dict[str, Any]) -> list[tuple[str, str | None]]:
    """The (code, field) of every violation, in order."""
    return [(violation["code"], violation["field"]) for violation in body["violations"]]
