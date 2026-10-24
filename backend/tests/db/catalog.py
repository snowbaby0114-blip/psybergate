"""What PostgreSQL actually holds, in a form that compares equal exactly when two schemas match.

Alembic's own bookkeeping table is left out: it is not part of the application schema.
Column order is left out too, because dropping and re-adding a column legitimately moves it.
"""

from sqlalchemy import Connection, text

_QUERIES = {
    "relations": """
        SELECT relname, relkind::text
        FROM pg_class
        WHERE relnamespace = 'public'::regnamespace AND relname NOT LIKE 'alembic_version%'
        ORDER BY relname
    """,
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid), a.attidentity::text
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
          AND c.relname <> 'alembic_version' AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attname
    """,
    "constraints": """
        SELECT conrelid::regclass::text, conname, contype::text, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE connamespace = 'public'::regnamespace
          AND conrelid::regclass::text <> 'alembic_version'
        ORDER BY conname
    """,
    "indexes": """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename <> 'alembic_version'
        ORDER BY indexname
    """,
    "extensions": "SELECT extname FROM pg_extension ORDER BY extname",
}


SchemaSnapshot = dict[str, list[tuple[object, ...]]]


def schema_snapshot(connection: Connection) -> SchemaSnapshot:
    return {
        name: [tuple(row) for row in connection.execute(text(sql))]
        for name, sql in _QUERIES.items()
    }
