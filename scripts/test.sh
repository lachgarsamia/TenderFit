#!/usr/bin/env bash
# Runs the whole backend suite against a throwaway pgvector database. A missing database fails the run rather than skipping tests.
set -euo pipefail
PORT=${TEST_DB_PORT:-55432}
if ! docker ps --format '{{.Names}}' | grep -q '^tenderfit-testdb$'; then
  docker rm -f tenderfit-testdb >/dev/null 2>&1 || true
  docker run -d --name tenderfit-testdb -e POSTGRES_USER=tenderfit -e POSTGRES_PASSWORD=tenderfit -e POSTGRES_DB=postgres \
    -p "127.0.0.1:$PORT:5432" pgvector/pgvector:pg16 >/dev/null
  until docker exec tenderfit-testdb pg_isready -U tenderfit >/dev/null 2>&1; do sleep 1; done
fi
export REQUIRE_DB=1
export TEST_ADMIN_DATABASE_URL="postgresql://tenderfit:tenderfit@127.0.0.1:$PORT/postgres"
export TEST_DATABASE_URL="postgresql://tenderfit:tenderfit@127.0.0.1:$PORT/tenderfit_test"
exec .venv/bin/python -m pytest -q -ra "$@"
