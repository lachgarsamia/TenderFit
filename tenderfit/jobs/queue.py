from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from tenderfit.db import Conn, must

STALE_AFTER = timedelta(hours=1)
# A what-if session is bounded by its deadline (simulation_agent_deadline_s, 150 s), so a running one this old has lost its
# worker. Closing it sooner than STALE_AFTER frees the supplier to ask again; with max_attempts=1 it fails, never reruns.
AGENT_STALE_AFTER = timedelta(minutes=10)


# Claimed after every ready job of any other kind: the authoritative work never waits behind a backlog of questions.
LOW_PRIORITY = ("simulation_agent",)


def enqueue(conn: Conn, kind: str, payload: dict | None = None, lock_key: str | None = None,
            run_after: datetime | None = None, max_attempts: int | None = None) -> int:
    """max_attempts=None keeps the table default (5); 1 means one attempt and no automatic retry."""
    if max_attempts is None:
        return must(conn.execute(
            "INSERT INTO job (kind, payload, lock_key, run_after) VALUES (%s, %s, %s, coalesce(%s, now())) RETURNING id",
            (kind, Jsonb(payload or {}), lock_key, run_after),
        ).fetchone())["id"]
    return must(conn.execute(
        """INSERT INTO job (kind, payload, lock_key, run_after, max_attempts)
           VALUES (%s, %s, %s, coalesce(%s, now()), %s) RETURNING id""",
        (kind, Jsonb(payload or {}), lock_key, run_after, max_attempts),
    ).fetchone())["id"]


def enqueue_unique(conn: Conn, kind: str, payload: dict, keys: tuple[str, ...], lock_key: str | None = None) -> int:
    """Reuse a job that is still waiting for the same target. A running job is not reused: it may predate this change."""
    match = {k: payload[k] for k in keys}
    existing = conn.execute("SELECT id FROM job WHERE kind = %s AND status = 'queued' AND payload @> %s ORDER BY id LIMIT 1",
                            (kind, Jsonb(match))).fetchone()
    return existing["id"] if existing else enqueue(conn, kind, payload, lock_key)


def claim(conn: Conn) -> dict | None:
    # A job whose worker died is retried only while it has attempts left; otherwise it is closed as failed.
    conn.execute(
        """UPDATE job SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END, locked_at = NULL,
                  last_error = coalesce(last_error, 'worker stopped while running this job'),
                  finished_at = CASE WHEN attempts >= max_attempts THEN now() END
           WHERE status = 'running' AND locked_at < now() - CASE WHEN kind = ANY(%s) THEN %s ELSE %s END""",
        (list(LOW_PRIORITY), AGENT_STALE_AFTER, STALE_AFTER),
    )
    job = conn.execute(
        """UPDATE job SET status = 'running', locked_at = now(), attempts = attempts + 1
           WHERE id = (
               SELECT id FROM job j WHERE status = 'queued' AND run_after <= now()
                 AND (lock_key IS NULL OR NOT EXISTS
                      (SELECT 1 FROM job r WHERE r.status = 'running' AND r.lock_key = j.lock_key))
               ORDER BY (kind = ANY(%s)), run_after, id FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING *""", (list(LOW_PRIORITY),)
    ).fetchone()
    conn.commit()
    return job


def complete(conn: Conn, job_id: int) -> None:
    conn.execute("UPDATE job SET status = 'done', finished_at = now(), last_error = NULL WHERE id = %s", (job_id,))
    conn.commit()


def fail(conn: Conn, job: dict, error: str) -> None:
    final = job["attempts"] >= job["max_attempts"]
    backoff = timedelta(seconds=min(30 * 2 ** job["attempts"], 3600))
    conn.execute(
        """UPDATE job SET status = %s, last_error = %s, locked_at = NULL, run_after = %s,
                          finished_at = CASE WHEN %s THEN now() END WHERE id = %s""",
        ("failed" if final else "queued", error[:2000], datetime.now(timezone.utc) + backoff, final, job["id"]),
    )
    conn.commit()


def pending(conn: Conn, kind: str) -> bool:
    return conn.execute("SELECT 1 FROM job WHERE kind = %s AND status IN ('queued', 'running') LIMIT 1",
                        (kind,)).fetchone() is not None


def waiting_behind(conn: Conn, job_id: int) -> int:
    """For a queued low-priority job: the jobs that will be claimed before it, in claim()'s order. Those running now,
    every ready job of another kind, and the low-priority jobs ahead of it. 0 once it is running or finished."""
    row = must(conn.execute(
        """SELECT count(*) AS n FROM job j, job me
           WHERE me.id = %s AND me.status = 'queued' AND j.id <> me.id AND (
                 j.status = 'running'
              OR (j.status = 'queued' AND j.run_after <= now() AND NOT (j.kind = ANY(%s)))
              OR (j.status = 'queued' AND j.kind = ANY(%s) AND (j.run_after, j.id) < (me.run_after, me.id)))""",
        (job_id, list(LOW_PRIORITY), list(LOW_PRIORITY))).fetchone())
    return int(row["n"])
