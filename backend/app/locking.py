"""Transaction-scoped advisory locks for the dispense transaction.

The data a quota check depends on is not a row that could be locked: it is "everything this patient
has had of this medicine", including rows that do not exist yet. An advisory lock names that set of
rows instead. PostgreSQL releases it when the transaction ends, however it ends, so no code path
can leave one behind.

The lock order of the dispense transaction (idempotency key, then medicine row, then patient and
medicine) is documented, and kept, in app.services.dispenses.
"""

import hashlib

from sqlalchemy import text
from sqlalchemy.orm import Session

# Free-text inputs refuse control characters, so no part of a key can contain the separator and
# the joined text is unambiguous: ("pair", 12, 3) and ("pair", 1, 23) are different keys.
_SEPARATOR = "\x1f"


def advisory_key(namespace: str, *parts: str | int) -> int:
    """The 64-bit key of an advisory lock: a hash of the namespace and the parts.

    Every user of advisory locks in a database shares one key space, so the namespace keeps the
    lock families apart. Two different names can still collide; the only effect of that is that
    their transactions wait for each other without need.

    The value is part of the locking protocol between application versions: if a release changed
    it, old and new instances would stop excluding each other during a rolling deployment.
    """
    name = _SEPARATOR.join([namespace, *map(str, parts)])
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def lock_idempotency_key(session: Session, key: str) -> None:
    """Serialise the requests that carry one idempotency key, so that a duplicate waits for the
    first one and then replays its outcome instead of racing it to the unique constraint."""
    _lock(session, advisory_key("idem", key))


def lock_patient_medicine(session: Session, medicine_id: int, patient_ref: str) -> None:
    """Serialise the quota checks of one patient for one medicine. Other patients and other
    medicines never wait on it."""
    _lock(session, advisory_key("pair", medicine_id, patient_ref))


def _lock(session: Session, key: int) -> None:
    # The cast pins the parameter's type: psycopg sends the narrowest integer type that fits the
    # value, which would otherwise change with the key's magnitude.
    session.execute(text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"), {"key": key})
