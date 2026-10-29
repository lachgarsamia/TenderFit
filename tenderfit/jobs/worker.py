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
    from tenderfit.changes.apply import pending_version_changes, record_status_changes
    out = incremental(conn, FtsClient(), backfill_days=payload.get("backfill_days", 14))
    out["status_changes"] = record_status_changes(conn)
    if pending_version_changes(conn) and not queue.pending(conn, "detect_changes"):
        queue.enqueue(conn, "detect_changes", lock_key="changes")
        conn.commit()
    return out


def run_assess_job(conn: psycopg.Connection, payload: dict) -> dict:
    from tenderfit.assessment.pipeline import assess_lot
    from tenderfit.llm import default_llm
    aid = assess_lot(conn, payload["lot_id"], payload["profile_id"], default_llm(), reason=payload.get("reason"))
    return {"assessment_id": aid}


def run_daily_check(conn: psycopg.Connection, payload: dict) -> dict:
    from tenderfit.profile.service import daily_check
    out = daily_check(conn, datetime.now(timezone.utc).date())
    for lot in out["lots"]:
        queue.enqueue(conn, "profile_change", {**lot, "facts": out["stale_facts"], "reason": "profile fact expired or overdue"},
                      lock_key=f"lot:{lot['lot_id']}")
    conn.commit()
    return out


def run_detect_changes(conn: psycopg.Connection, payload: dict) -> dict:
    from tenderfit.changes.apply import process_pending
    from tenderfit.llm import default_llm
    return {"impacts": process_pending(conn, default_llm())}


def run_shadow_full(conn: psycopg.Connection, payload: dict) -> dict:
    from tenderfit.changes.apply import shadow_full
    from tenderfit.llm import default_llm
    return shadow_full(conn, payload["impact_id"], default_llm())


def run_profile_change(conn: psycopg.Connection, payload: dict) -> dict:
    from tenderfit.changes.apply import apply_profile_change
    from tenderfit.llm import default_llm
    aid = apply_profile_change(conn, payload["lot_id"], payload["profile_id"], payload["facts"], default_llm(),
                               payload.get("reason", "profile fact changed"))
    return {"assessment_id": aid}


HANDLERS = {"import": run_import_job, "assess": run_assess_job, "daily_check": run_daily_check,
            "detect_changes": run_detect_changes, "shadow_full": run_shadow_full, "profile_change": run_profile_change}


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
