from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (names are case-insensitive)."""

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/emguidance"
    db_pool_size: Annotated[int, Field(gt=0)] = 10
    db_max_overflow: Annotated[int, Field(ge=0)] = 20
    # PostgreSQL treats 0 as "no timeout", which would silently disable the protection
    # the 503 resource_busy path relies on, so zero is rejected rather than passed through.
    lock_timeout_ms: Annotated[int, Field(gt=0)] = 5000
    statement_timeout_ms: Annotated[int, Field(gt=0)] = 15000
    log_level: LogLevel = "INFO"

    @field_validator("database_url")
    @classmethod
    def _require_psycopg3_driver(cls, value: str) -> str:
        # A bare postgresql:// URL makes SQLAlchemy look for psycopg2, which is not installed.
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("must use the psycopg3 driver: postgresql+psycopg://user:pass@host/db")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
