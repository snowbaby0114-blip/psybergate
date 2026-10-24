"""Opaque keyset cursors: the sort key of the last item of a page plus a fingerprint of the
filters the page was requested with, so a cursor cannot be replayed against a different query.

The token is not signed. A client can forge any key, so callers must validate the arity and types
of the key they get back and treat a bad one as an invalid cursor.
"""

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

_CURSOR_VERSION = 1


class InvalidCursor(Exception):
    pass


def filter_fingerprint(filters: Mapping[str, Any]) -> str:
    """Callers must build `filters` the same way when issuing and when checking a cursor: a missing
    key and a null value are different filters."""
    canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def encode_cursor(key: Sequence[Any], filters: Mapping[str, Any]) -> str:
    payload = {"v": _CURSOR_VERSION, "k": list(key), "f": filter_fingerprint(filters)}
    return _to_base64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def decode_cursor(token: str, filters: Mapping[str, Any]) -> list[Any]:
    payload = _parse(token)
    if not isinstance(payload, dict) or payload.keys() != {"v", "k", "f"}:
        raise InvalidCursor("cursor has an unexpected structure")
    version, key, fingerprint = payload["v"], payload["k"], payload["f"]
    if type(version) is not int or version != _CURSOR_VERSION:
        raise InvalidCursor(f"unsupported cursor version {version!r}")
    if not isinstance(key, list):
        raise InvalidCursor("cursor has an unexpected structure")
    if fingerprint != filter_fingerprint(filters):
        raise InvalidCursor("cursor was issued for different filters")
    return key


def _to_base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _parse(token: str) -> Any:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError as exc:
        raise InvalidCursor("cursor is not valid base64url") from exc
    # urlsafe_b64decode silently skips characters outside the alphabet, so only accept the exact
    # spelling that encode_cursor produces.
    if _to_base64url(raw) != token:
        raise InvalidCursor("cursor is not valid base64url")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise InvalidCursor("cursor does not contain valid JSON") from exc
