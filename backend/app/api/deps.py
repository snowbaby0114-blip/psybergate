"""FastAPI dependencies: the request's database session, clock, settings and business date."""

from collections.abc import Callable, Iterator
from datetime import date, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.timeutil import to_local_date

# Returns the current instant as an aware UTC datetime. Injectable so tests control "now".
Clock = Callable[[], datetime]


def get_session(request: Request) -> Iterator[Session]:
    # One session per request, closed (and any open transaction rolled back) however it ends.
    # Services open and commit their own transactions; nothing here commits.
    with request.app.state.session_factory() as session:
        yield session


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


SessionDep = Annotated[Session, Depends(get_session)]
ClockDep = Annotated[Clock, Depends(get_clock)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_today(clock: ClockDep) -> date:
    """Today as a SAST business date, never the UTC date."""
    return to_local_date(clock())


TodayDep = Annotated[date, Depends(get_today)]
