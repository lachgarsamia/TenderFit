import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from tenderfit.config import settings

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or settings.database_url, row_factory=dict_row)


def migrate(conn: psycopg.Connection) -> list[str]:
    conn.execute("SELECT pg_advisory_xact_lock(42)")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migration (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migration")}
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        conn.execute(path.read_text())
        conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    conn.commit()
    return applied


if __name__ == "__main__" and sys.argv[1:] == ["migrate"]:
    with connect() as c:
        print("applied:", migrate(c) or "nothing")
