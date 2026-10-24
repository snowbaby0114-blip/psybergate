from fastapi import APIRouter

from app import repositories
from app.api.deps import SessionDep
from app.api.responses import problem_responses
from app.schemas import HealthOut

router = APIRouter(tags=["ops"])


@router.get(
    "/healthz",
    summary="Liveness and readiness",
    description="Answers 200 only if the database can run a query.",
    responses=problem_responses(503),
)
def healthz(session: SessionDep) -> HealthOut:
    repositories.ping(session)
    return HealthOut(status="ok")
