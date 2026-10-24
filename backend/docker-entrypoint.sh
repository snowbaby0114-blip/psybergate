#!/bin/sh
# `serve` (the image's default command) migrates the database and starts the API. Any other
# command runs unchanged: pytest, python -m app.seed, alembic downgrade -1, a shell.
set -eu

if [ "${1:-}" = "serve" ]; then
    if [ -z "${DATABASE_URL:-}" ]; then
        echo "docker-entrypoint: DATABASE_URL is not set" >&2
        echo "  expected: postgresql+psycopg://user:password@host:5432/database" >&2
        exit 1
    fi

    alembic upgrade head

    # One process, no reload. The proxy (nginx, another container whose address is not known
    # in advance) is the only caller whose forwarded scheme and client address matter.
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'
fi

exec "$@"
