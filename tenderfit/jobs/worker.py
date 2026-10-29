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


def run_assess_job(conn: psycopg.Connection, payload: dict) -> dict:
    from tenderfit.assessment.pipeline import assess_lot
    from tenderfit.llm import default_llm
    aid = assess_lot(conn, payload["lot_id"], payload["profile_id"], default_llm(), reason=payload.get("reason"))
    return {"assessment_id": aid}


def run_daily_check(conn: psycopg.Connection, payload: dict) -> dict:
    from tenderfit.profile.service import daily_check
    out = daily_check(conn, datetime.now(timezone.utc).date())
    for lot in out["lots"]:
        queue.enqueue(conn, "assess", {**lot, "reason": "profile fact expired or overdue"}, lock_key=f"lot:{lot['lot_id']}")
    conn.commit()
    return out


HANDLERS = {"import": run_import_job, "assess": run_assess_job, "daily_check": run_daily_check}


def schedule_daily_check(conn: psycopg.Connection, now: datetime) -> int | None:
    if queue.pending(conn, "daily_check") or conn.execute(
            "SELECT 1 FROM job WHERE kind = 'daily_check' AND created_at > %s LIMIT 1", (now - timedelta(days=1),)).fetchone():
        return None
    job_id = queue.enqueue(conn, "daily_check")
    conn.commit()
    return job_id


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
    schedule_daily_check(conn, datetime.now(timezone.utc))
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
