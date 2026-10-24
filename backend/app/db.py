from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

# libpq connect_timeout (it enforces a 2 s minimum). Without a limit, an unreachable database
# blocks a request, the readiness probe or a test run until the operating system gives up,
# which takes minutes.
CONNECT_TIMEOUT_SECONDS = 5


def create_db_engine(
    url: str | None = None,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    statement_timeout_ms: int | None = None,
) -> Engine:
    settings = get_settings()
    if pool_size is None:
        pool_size = settings.db_pool_size
    if max_overflow is None:
        max_overflow = settings.db_max_overflow
    if statement_timeout_ms is None:
        statement_timeout_ms = settings.statement_timeout_ms
    return create_engine(
        url or settings.database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        # Drops connections the server closed (restart, failover, idle kill) instead of
        # surfacing them as a failed first request after the outage.
        pool_pre_ping=True,
        # timezone=UTC: every timestamptz leaves the database as a UTC instant; the business
        # zone (Africa/Johannesburg) is applied only in app.domain.timeutil.
        connect_args={
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "options": f"-c timezone=UTC -c statement_timeout={statement_timeout_ms}",
        },
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: services commit and then build responses from the same objects,
    # which must not trigger a hidden SELECT per attribute. autoflush=False: writes happen
    # exactly where the service code says so, never as a side effect of a read.
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)
