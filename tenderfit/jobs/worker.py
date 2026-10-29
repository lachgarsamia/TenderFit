import logging
import time
import traceback
from datetime import datetime, timedelta, timezone

import psycopg

from tenderfit.config import settings
from tenderfit.db import connect, migrate
from tenderfit.ingest.client import FtsClient
from tenderfit.ingest.importer import SOURCE, incremental
from tenderfit.jobs import queue

log = logging.getLogger("tenderfit.worker")


def run_import_job(conn: psycopg.Connection, payload: dict) -> dict:
    return incremental(conn, FtsClient(), backfill_days=payload.get("backfill_days", 14))


HANDLERS = {"import": run_import_job}


def schedule_import(conn: psycopg.Connection, now: datetime) -> int | None:
    if queue.pending(conn, "import"):
        return None
    wm = conn.execute("SELECT watermark FROM import_state WHERE source = %s", (SOURCE,)).fetchone()
    if wm and now - wm["watermark"] < timedelta(hours=settings.import_interval_hours):
        return None
    job_id = queue.enqueue(conn, "import", lock_key="import")
    conn.commit()
    return job_id


def run_once(conn: psycopg.Connection) -> bool:
    schedule_import(conn, datetime.now(timezone.utc))
    job = queue.claim(conn)
    if not job:
        return False
    try:
        result = HANDLERS[job["kind"]](conn, job["payload"])
        queue.complete(conn, job["id"])
        log.info("job %s %s done: %s", job["id"], job["kind"], result)
    except Exception:
        conn.rollback()
        queue.fail(conn, job, traceback.format_exc())
        log.exception("job %s %s failed", job["id"], job["kind"])
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with connect() as conn:
        migrate(conn)
        while True:
            if not run_once(conn):
                time.sleep(10)


if __name__ == "__main__":
    main()
