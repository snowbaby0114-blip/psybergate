"""Fingerprint of a dispense request: tells a replay of the same request from a key reused with
a different payload. The idempotency key itself is deliberately not part of it."""

import hashlib
import json
from datetime import datetime

from app.domain.timeutil import ensure_utc


def request_fingerprint(
    *,
    medicine_code: str,
    patient_ref: str,
    quantity: int,
    dispensed_at: datetime | None,
    authorisation_ref: str | None,
) -> str:
    payload = {
        "medicine_code": medicine_code,
        "patient_ref": patient_ref,
        "quantity": quantity,
        # An omitted dispensed_at means "now" on the server, which is a different request from any
        # explicit instant, so it is hashed as null instead of being resolved first.
        "dispensed_at": None if dispensed_at is None else ensure_utc(dispensed_at).isoformat(),
        "authorisation_ref": _blank_to_none(authorisation_ref),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None
