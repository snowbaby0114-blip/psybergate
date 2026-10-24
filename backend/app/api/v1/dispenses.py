from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from app import errors
from app.api.deps import ClockDep, SessionDep, SettingsDep
from app.api.responses import problem_responses
from app.schemas import DispenseCreate, DispenseOut, MedicineCode, Page, PatientRef
from app.services import dispenses

router = APIRouter(prefix="/dispenses", tags=["dispenses"])

REPLAYED_HEADER = "Idempotency-Replayed"

_REPLAYED_HEADER_DOC: dict[str, Any] = {
    REPLAYED_HEADER: {
        "description": "true when the idempotency key was already used and this is the original "
        "outcome again; nothing new was recorded.",
        "schema": {"type": "string", "enum": ["true"]},
    }
}


def _responses() -> dict[int | str, dict[str, Any]]:
    documented = problem_responses(422, 503)
    documented[422] = {**documented[422], "headers": _REPLAYED_HEADER_DOC}
    return {
        201: {
            "description": "The dispense was recorded, or this is the original outcome of the key.",
            "headers": _REPLAYED_HEADER_DOC,
        },
        **documented,
    }


@router.post(
    "",
    status_code=201,
    summary="Record a dispense, idempotently",
    description=(
        "Validated against the rule in force on dispensed_at, which may be in the past. A "
        "rejection (422 dispense_rejected) lists every violated rule and is kept in the attempt "
        "log, but records no dispense. Repeating a request with the same idempotency_key and "
        "payload returns the original status and body again, marked by Idempotency-Replayed: true; "
        "the same key with a different payload is a 422 idempotency_key_reuse."
    ),
    responses=_responses(),
)
def create_dispense(
    body: DispenseCreate,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    response: Response,
) -> DispenseOut:
    result = dispenses.create_dispense(
        session, body, now=clock(), lock_timeout_ms=settings.lock_timeout_ms
    )
    replay_headers = {REPLAYED_HEADER: "true"} if result.replayed else {}
    if isinstance(result, dispenses.Rejected):
        problem = errors.dispense_rejected(result.violations, result.attempt_id)
        problem.headers.update(replay_headers)
        raise problem
    response.headers.update(replay_headers)
    return result.dispense


@router.get(
    "",
    summary="List dispenses, newest first",
    description=(
        "Ordered by dispensed_at, then id, both descending. Both filters match exactly; a "
        "medicine code that does not exist gives an empty page. Page with next_cursor, never "
        "with offsets."
    ),
    responses=problem_responses(400, 422),
)
def list_dispenses(
    session: SessionDep,
    patient_ref: Annotated[PatientRef | None, Query(description="Only this patient.")] = None,
    medicine: Annotated[MedicineCode | None, Query(description="Only this medicine code.")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size.")] = 25,
    cursor: Annotated[
        str | None,
        Query(description="The next_cursor of the previous page, for the same filters."),
    ] = None,
) -> Page[DispenseOut]:
    return dispenses.list_dispenses(
        session, patient_ref=patient_ref, medicine=medicine, limit=limit, cursor=cursor
    )
