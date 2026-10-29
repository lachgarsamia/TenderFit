from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

STALE_AFTER = timedelta(hours=1)


def enqueue(conn: psycopg.Connection, kind: str, payload: dict | None = None, lock_key: str | None = None,
            run_after: datetime | None = None) -> int:
    return conn.execute(
        "INSERT INTO job (kind, payload, lock_key, run_after) VALUES (%s, %s, %s, coalesce(%s, now())) RETURNING id",
        (kind, Jsonb(payload or {}), lock_key, run_after),
    ).fetchone()["id"]


def claim(conn: psycopg.Connection) -> dict | None:
    conn.execute(
        "UPDATE job SET status = 'queued', locked_at = NULL WHERE status = 'running' AND locked_at < now() - %s",
        (STALE_AFTER,),
    )
    job = conn.execute(
        """UPDATE job SET status = 'running', locked_at = now(), attempts = attempts + 1
           WHERE id = (
               SELECT id FROM job j WHERE status = 'queued' AND run_after <= now()
                 AND (lock_key IS NULL OR NOT EXISTS
                      (SELECT 1 FROM job r WHERE r.status = 'running' AND r.lock_key = j.lock_key))
               ORDER BY run_after, id FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING *"""
    ).fetchone()
    conn.commit()
    return job


def complete(conn: psycopg.Connection, job_id: int) -> None:
    conn.execute("UPDATE job SET status = 'done', finished_at = now(), last_error = NULL WHERE id = %s", (job_id,))
    conn.commit()


def fail(conn: psycopg.Connection, job: dict, error: str) -> None:
    final = job["attempts"] >= job["max_attempts"]
    backoff = timedelta(seconds=min(30 * 2 ** job["attempts"], 3600))
    conn.execute(
        """UPDATE job SET status = %s, last_error = %s, locked_at = NULL, run_after = %s,
                          finished_at = CASE WHEN %s THEN now() END WHERE id = %s""",
        ("failed" if final else "queued", error[:2000], datetime.now(timezone.utc) + backoff, final, job["id"]),
    )
    conn.commit()


def pending(conn: psycopg.Connection, kind: str) -> bool:
    return conn.execute("SELECT 1 FROM job WHERE kind = %s AND status IN ('queued', 'running') LIMIT 1",
                        (kind,)).fetchone() is not None
