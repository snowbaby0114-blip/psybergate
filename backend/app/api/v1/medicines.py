from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Path, Query, Request, Response

from app.api.deps import SessionDep, SettingsDep, TodayDep
from app.api.responses import problem_responses
from app.schemas import (
    MedicineCode,
    MedicineDetail,
    MedicineOut,
    Page,
    RuleCreate,
    RuleListOut,
    RuleSupersedeResult,
    SearchText,
)
from app.services import formulary, medicines

router = APIRouter(prefix="/medicines", tags=["medicines"])

CodePath = Annotated[MedicineCode, Path(description="The medicine's unique code.")]


@router.get(
    "",
    summary="Search the medicine catalogue",
    description="Ordered by name, then code. Page with next_cursor, never with offsets.",
    responses=problem_responses(400, 422),
)
def list_medicines(
    session: SessionDep,
    q: Annotated[
        SearchText | None,
        Query(description="Case-insensitive part of the medicine's name or code."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size.")] = 25,
    cursor: Annotated[
        str | None,
        Query(description="The next_cursor of the previous page, for the same q."),
    ] = None,
) -> Page[MedicineOut]:
    return medicines.search_medicines(session, q=q, limit=limit, cursor=cursor)


@router.get(
    "/{code}",
    summary="Get a medicine with the rule in force today",
    responses=problem_responses(404, 422),
)
def get_medicine(code: CodePath, session: SessionDep, today: TodayDep) -> MedicineDetail:
    return medicines.get_medicine_detail(session, code, today)


@router.get(
    "/{code}/rules",
    summary="Get the full rule history of a medicine",
    responses=problem_responses(404, 422),
)
def list_rules(code: CodePath, session: SessionDep, today: TodayDep) -> RuleListOut:
    return formulary.list_rules(session, code, today)


@router.post(
    "/{code}/rules",
    status_code=201,
    summary="Introduce a rule from a date, superseding what is in force",
    description=(
        "The rule in force on effective_from is cut short there and the new rule inherits its "
        "end; if effective_from falls between rules, the new rule runs until the next one "
        "starts. Rules are never deleted or rewritten, so this fails if a rule already starts "
        "on effective_from or if recorded dispenses would be governed by a different rule."
    ),
    responses=problem_responses(404, 409, 422, 503),
)
def supersede_rule(
    code: CodePath,
    body: RuleCreate,
    session: SessionDep,
    settings: SettingsDep,
    today: TodayDep,
    request: Request,
    response: Response,
) -> RuleSupersedeResult:
    result = formulary.supersede_rule(
        session, code, body, today=today, lock_timeout_ms=settings.lock_timeout_ms
    )
    # The new rule has no URL of its own; the collection that now lists it is the closest.
    response.headers["Location"] = str(
        request.app.url_path_for("list_rules", code=quote(code, safe=""))
    )
    return result
