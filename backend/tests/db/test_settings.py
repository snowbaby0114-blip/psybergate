"""Settings: defaults from the SPEC, environment overrides, and values that must be rejected."""

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings

ENVIRONMENT_VARIABLES = (
    "DATABASE_URL",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "LOCK_TIMEOUT_MS",
    "STATEMENT_TIMEOUT_MS",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No setting from the developer's shell leaks in, and the cached settings do not leak out."""
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_match_the_spec() -> None:
    settings = Settings()
    assert settings.model_dump() == {
        "database_url": "postgresql+psycopg://postgres:postgres@localhost:5432/emguidance",
        "db_pool_size": 10,
        "db_max_overflow": 20,
        "lock_timeout_ms": 5000,
        "statement_timeout_ms": 15000,
        "log_level": "INFO",
    }


def test_environment_variables_override_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/other")
    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("LOCK_TIMEOUT_MS", "250")
    monkeypatch.setenv("STATEMENT_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings().model_dump() == {
        "database_url": "postgresql+psycopg://u:p@db:5432/other",
        "db_pool_size": 3,
        "db_max_overflow": 0,
        "lock_timeout_ms": 250,
        "statement_timeout_ms": 1000,
        "log_level": "DEBUG",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOCK_TIMEOUT_MS", "0"),
        ("STATEMENT_TIMEOUT_MS", "0"),
        ("DB_POOL_SIZE", "0"),
        ("DB_MAX_OVERFLOW", "-1"),
        ("DATABASE_URL", "postgresql://postgres:postgres@localhost/emguidance"),
        ("LOG_LEVEL", "LOUD"),
    ],
)
def test_values_that_would_misconfigure_the_service_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError) as raised:
        Settings()
    assert name.lower() in str(raised.value)


def test_get_settings_reads_the_environment_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "4")
    first = get_settings()
    monkeypatch.setenv("DB_POOL_SIZE", "5")
    assert get_settings() is first
    assert first.db_pool_size == 4

    get_settings.cache_clear()
    assert get_settings().db_pool_size == 5
