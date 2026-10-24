"""OpenAPI documentation of the error responses, which are all described by ProblemOut.

Declaring 422 explicitly also stops FastAPI from adding its own default 422 schema
(HTTPValidationError), which does not match our error contract.
"""

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI

from app.errors import PROBLEM_MEDIA_TYPE
from app.schemas import ProblemOut

_DESCRIPTIONS = {
    400: "Invalid cursor.",
    404: "The medicine does not exist.",
    409: "The request conflicts with the rules already on record.",
    422: "The request is invalid, or a well-formed request was rejected by the rules.",
    503: "The resource is busy or the service is unavailable; retry shortly.",
}


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {
        status: {"model": ProblemOut, "description": _DESCRIPTIONS[status]} for status in statuses
    }


def _error_responses(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            for status, response in operation["responses"].items():
                if status.isdigit() and int(status) >= 400:
                    yield response


def document_problem_media_type(app: FastAPI) -> None:
    """Describe error responses as application/problem+json, the type they are sent as.

    FastAPI files every additional response under the route's own media type
    (application/json) and offers no per-response override, so the generated schema is adjusted
    once, where it is first built.
    """
    build_schema = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            for response in _error_responses(build_schema()):
                content = response.get("content", {})
                if "application/json" in content:
                    response["content"] = {PROBLEM_MEDIA_TYPE: content["application/json"]}
        return app.openapi_schema

    app.openapi = openapi
