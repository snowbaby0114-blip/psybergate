"""The application factory.

`create_app` is the single place that wires settings, database, middleware, error handlers and
routes, and tests call it with their own engine and clock. The module-level `app` is what
uvicorn serves; creating it does not connect to the database, because the engine connects lazily.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy import Engine

from app.api import health
from app.api.deps import Clock
from app.api.request_id import RequestIdMiddleware
from app.api.responses import document_problem_media_type
from app.api.v1 import router as v1_router
from app.config import Settings, get_settings
from app.db import create_db_engine, create_session_factory
from app.errors import register_exception_handlers

API_DESCRIPTION = """\
A pharmacy formulary whose rules change over time, and the dispenses validated against them.

* A dispense is judged by the rule in force on **its own date**, not on the day it is recorded.
* Instants are ISO-8601 in UTC (`2026-03-15T10:00:00Z`). A **business day** is a calendar date in
  Africa/Johannesburg (SAST); `effective_to` of a rule is the first day it no longer applies.
* Listings use keyset pagination: pass `next_cursor` back as `cursor`, with the same filters.
* Every error is `application/problem+json` with a `violations` list; `violations[].field` names
  the request field that caused each one, `null` when it is not about one field.
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def configure_logging(level: str) -> None:
    # Only the application's own loggers: raising the root logger to INFO would also make
    # SQLAlchemy log every statement.
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        app_logger.addHandler(handler)


def create_app(
    *,
    engine: Engine | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Without an `engine`, the lifespan creates one from the settings and disposes it on
    shutdown; an injected engine belongs to the caller."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        owned_engine = None
        if engine is None:
            owned_engine = create_db_engine(
                settings.database_url,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                statement_timeout_ms=settings.statement_timeout_ms,
            )
            app.state.engine = owned_engine
            app.state.session_factory = create_session_factory(owned_engine)
        try:
            yield
        finally:
            if owned_engine is not None:
                owned_engine.dispose()

    app = FastAPI(
        title="EMGuidance formulary and dispensing API",
        description=API_DESCRIPTION,
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
        # A redirect from /x/ to /x would be a non-2xx response outside the error contract.
        redirect_slashes=False,
    )
    app.state.settings = settings
    app.state.clock = clock or utc_now
    if engine is not None:
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)

    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(v1_router)
    document_problem_media_type(app)
    return app


app = create_app()
