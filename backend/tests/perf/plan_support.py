"""Reading what PostgreSQL does with the statements the repositories run.

Nothing here re-creates a query: a repository function runs as a service runs it, the SQL and
parameters that reach the driver are recorded, and PostgreSQL is asked to EXPLAIN exactly those.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, event
from sqlalchemy.orm import Session

from app.db import create_db_engine

Query = Callable[[Session], object]


@dataclass(frozen=True)
class Statement:
    """What reached the driver: SQL with %(name)s placeholders, and the values bound to them."""

    sql: str
    parameters: dict[str, Any]


class Plan:
    """An EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) document."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.root: dict[str, Any] = document["Plan"]
        self.nodes: list[dict[str, Any]] = []
        self._collect(self.root)

    def _collect(self, node: dict[str, Any]) -> None:
        self.nodes.append(node)
        for child in node.get("Plans", []):
            self._collect(child)

    @property
    def indexes(self) -> set[str]:
        return {node["Index Name"] for node in self.nodes if "Index Name" in node}

    @property
    def sequentially_scanned(self) -> set[str]:
        return {node["Relation Name"] for node in self.nodes if node["Node Type"] == "Seq Scan"}

    @property
    def sorts(self) -> int:
        return sum(node["Node Type"] == "Sort" for node in self.nodes)

    @property
    def buffers(self) -> int:
        """Pages of tables and indexes the execution touched (only an ANALYZE plan has them)."""
        return self.root["Shared Hit Blocks"] + self.root["Shared Read Blocks"]

    def index_node(self, index: str) -> dict[str, Any]:
        (node,) = (node for node in self.nodes if node.get("Index Name") == index)
        return node


@contextmanager
def _recorded(engine: Engine) -> Iterator[list[Statement]]:
    statements: list[Statement] = []

    def record(connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        statements.append(Statement(statement, dict(parameters)))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def select_run_by(engine: Engine, query: Query) -> Statement:
    """Run `query` in a session and transaction of its own, as a service does, and return the
    one SELECT it sent to the database."""
    with _recorded(engine) as statements, Session(engine) as session, session.begin():
        query(session)
    (select,) = (s for s in statements if s.sql.lstrip().upper().startswith("SELECT"))
    return select


def explain(engine: Engine, statement: Statement) -> Plan:
    """Execute the statement under EXPLAIN ANALYZE with its parameters bound, which is what the
    first executions of a statement are: planned for the values they are given."""
    with engine.connect() as connection:
        (document,) = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement.sql, statement.parameters
        ).scalar_one()
    return Plan(document)


@dataclass(frozen=True)
class PreparedStatement:
    """A statement as psycopg prepared it on the connection, with the plans PostgreSQL has used."""

    sql: str
    custom_plans: int
    generic_plans: int


@contextmanager
def _connection_of_its_own(engine: Engine) -> Iterator[Connection]:
    """A connection that has run nothing yet. One from the engine's pool may carry statements
    prepared by earlier tests, which would be counted as this test's."""
    private = create_db_engine(
        engine.url.render_as_string(hide_password=False), pool_size=1, max_overflow=0
    )
    try:
        with private.connect() as connection:
            yield connection
    finally:
        private.dispose()


def run_repeatedly(engine: Engine, query: Query, times: int) -> tuple[PreparedStatement, Plan]:
    """Run `query` `times` times on one connection, each time in a transaction of its own, then
    report how psycopg and PostgreSQL treated its SELECT and what its generic plan is.

    psycopg prepares a statement on the server after it has been executed five times on a
    connection, and PostgreSQL then plans it for the first five executions with the values given
    and afterwards may settle on one generic plan that ignores the values. The generic plan is
    asked for explicitly, because whether PostgreSQL adopts it is a matter of cost estimates.
    """
    with _connection_of_its_own(engine) as connection:
        for _ in range(times):
            with Session(bind=connection) as session, session.begin():
                query(session)
        driver = connection.connection.driver_connection
        assert driver is not None
        rows = driver.execute(
            "SELECT statement, custom_plans, generic_plans FROM pg_prepared_statements "
            "WHERE statement LIKE 'SELECT%' AND statement LIKE '%FROM%'"
        ).fetchall()
        assert len(rows) == 1, f"expected one prepared SELECT, found {len(rows)}"
        sql, custom_plans, generic_plans = rows[0]
        (document,) = driver.execute("EXPLAIN (GENERIC_PLAN, FORMAT JSON) " + sql).fetchone()[0]
        driver.rollback()
    return PreparedStatement(sql, custom_plans, generic_plans), Plan(document)
