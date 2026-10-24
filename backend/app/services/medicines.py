"""The medicine catalogue: search with keyset pagination, and one medicine with its current rule."""

from collections.abc import Mapping
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app import errors, repositories
from app.pagination import InvalidCursor, decode_cursor, encode_cursor
from app.schemas import MedicineDetail, MedicineOut, Page
from app.services import formulary


def search_medicines(
    session: Session, *, q: str | None, limit: int, cursor: str | None
) -> Page[MedicineOut]:
    term = q or None
    # The cursor is bound to the search term: reusing it with another term would resume from a
    # position in a different result set.
    filters = {"q": term}
    after = _decode_position(cursor, filters)
    with session.begin():
        rows = repositories.search_medicines(session, term, after, limit)
    page = rows[:limit]
    next_cursor = (
        encode_cursor([page[-1].name, page[-1].code], filters) if len(rows) > limit else None
    )
    return Page[MedicineOut](
        items=[MedicineOut.model_validate(row) for row in page], next_cursor=next_cursor
    )


def get_medicine_detail(session: Session, code: str, today: date) -> MedicineDetail:
    with session.begin():
        medicine = repositories.get_medicine_by_code(session, code)
        if medicine is None:
            raise errors.medicine_not_found(code)
        rows = repositories.list_rules(session, medicine.id)
    current = formulary.rule_in_force_on(rows, today)
    return MedicineDetail(
        **dict(MedicineOut.model_validate(medicine)),
        as_of=today,
        current_rule=formulary.rule_out(current, today) if current is not None else None,
    )


def _decode_position(cursor: str | None, filters: Mapping[str, Any]) -> tuple[str, str] | None:
    if cursor is None:
        return None
    try:
        key = decode_cursor(cursor, filters)
    except InvalidCursor as exc:
        raise errors.invalid_cursor() from exc
    # Cursors are not signed, so what they hold is client input: check its shape, and that
    # PostgreSQL can bind it (NUL, or a lone surrogate that cannot be encoded, would be a 500).
    if len(key) == 2 and all(isinstance(part, str) and _is_bindable(part) for part in key):
        return key[0], key[1]
    raise errors.invalid_cursor()


def _is_bindable(text: str) -> bool:
    if "\x00" in text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
