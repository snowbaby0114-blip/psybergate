"""Request correlation: every response carries X-Request-ID, and error bodies repeat it."""

import re
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"

# A client may supply its own id so one trace spans its logs and ours, but only a plain token:
# anything else (long, spaces, control characters) would end up verbatim in logs and responses.
_ACCEPTED_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


def new_request_id() -> str:
    return uuid.uuid4().hex


def request_id_of(conn: HTTPConnection) -> str:
    """The id of the current request. RequestIdMiddleware sets it first; the fallback only
    covers code that runs without the middleware, and stays stable for the rest of the request."""
    state = conn.scope.setdefault("state", {})
    return state.setdefault("request_id", new_request_id())


def _client_request_id(scope: Scope) -> str | None:
    candidate = Headers(scope=scope).get(REQUEST_ID_HEADER)
    if candidate is not None and _ACCEPTED_CLIENT_ID.fullmatch(candidate):
        return candidate
    return None


class RequestIdMiddleware:
    """Pure ASGI rather than BaseHTTPMiddleware, which would run the app in a separate task."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _client_request_id(scope) or new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)
