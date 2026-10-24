"""The rule engine and the cursor codec stay pure: standard library only, no framework, ORM or
other application module, so they can be tested without a database and reused anywhere."""

import ast
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
DOMAIN_PACKAGE = "app.domain"
PURE_MODULES = [
    *sorted((BACKEND / "app" / "domain").rglob("*.py")),
    BACKEND / "app" / "pagination.py",
]


def imported_modules(source: str, package: str) -> list[str]:
    """Absolute names of every module the source imports, wherever the import statement sits
    (module level, inside a function or under `if TYPE_CHECKING`). `package` is the package the
    source lives in and is only needed to resolve relative imports."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                anchor = parts[: len(parts) - (node.level - 1)]
                names.append(".".join([*anchor, *([node.module] if node.module else [])]))
            else:
                names.append(node.module or "")
    return names


def forbidden_imports(source: str, package: str) -> list[str]:
    return [
        name
        for name in imported_modules(source, package)
        if name.split(".")[0] not in sys.stdlib_module_names
        and name != DOMAIN_PACKAGE
        and not name.startswith(f"{DOMAIN_PACKAGE}.")
    ]


def package_of(path: Path) -> str:
    return ".".join(path.relative_to(BACKEND).with_suffix("").parts[:-1])


@pytest.mark.parametrize(
    "path", PURE_MODULES, ids=lambda path: path.relative_to(BACKEND).as_posix()
)
def test_pure_module_imports_only_the_standard_library(path: Path) -> None:
    offenders = forbidden_imports(path.read_text(encoding="utf-8"), package_of(path))

    assert offenders == []


def test_every_module_of_the_domain_package_and_the_cursor_codec_is_checked() -> None:
    checked = {path.relative_to(BACKEND).as_posix() for path in PURE_MODULES}

    assert checked >= {
        "app/domain/timeutil.py",
        "app/domain/rules.py",
        "app/domain/dispense.py",
        "app/domain/idempotency.py",
        "app/pagination.py",
    }


class TestTheCheckItself:
    @pytest.mark.parametrize(
        ("source", "offender"),
        [
            pytest.param("import sqlalchemy", "sqlalchemy", id="import"),
            pytest.param("from sqlalchemy.orm import Session", "sqlalchemy.orm", id="from import"),
            pytest.param("import fastapi.routing", "fastapi.routing", id="dotted import"),
            pytest.param("from pydantic import BaseModel", "pydantic", id="pydantic"),
            pytest.param("import tzdata", "tzdata", id="the zone database package"),
            pytest.param("from app.models import Medicine", "app.models", id="application module"),
            pytest.param("from app import models", "app", id="application package"),
            pytest.param("import app.db", "app.db", id="application module by import"),
            pytest.param("from .. import models", "app", id="relative import out of the package"),
            pytest.param("from ..config import settings", "app.config", id="relative module"),
            pytest.param("def f():\n    import psycopg", "psycopg", id="inside a function"),
            pytest.param(
                "if TYPE_CHECKING:\n    from sqlalchemy.orm import Session",
                "sqlalchemy.orm",
                id="under TYPE_CHECKING",
            ),
        ],
    )
    def test_it_flags_anything_that_is_not_standard_library_or_domain(
        self, source: str, offender: str
    ) -> None:
        assert forbidden_imports(source, DOMAIN_PACKAGE) == [offender]

    @pytest.mark.parametrize(
        "source",
        [
            "import json, hashlib",
            "from datetime import UTC, datetime",
            "from zoneinfo import ZoneInfo",
            "from collections.abc import Sequence",
            "import os.path",
            "from __future__ import annotations",
            "from app.domain.timeutil import ensure_utc",
            "import app.domain.rules",
            "from app.domain import rules",
            "from . import rules",
            "from .rules import RuleVersion",
        ],
    )
    def test_it_allows_the_standard_library_and_the_domain_package_itself(
        self, source: str
    ) -> None:
        assert forbidden_imports(source, DOMAIN_PACKAGE) == []
