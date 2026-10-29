from tenderfit.db import connect
from tenderfit.jobs import queue


def test_same_lock_key_runs_one_at_a_time(db, _test_db):
    queue.enqueue(db, "import", lock_key="import")
    queue.enqueue(db, "import", lock_key="import")
    queue.enqueue(db, "other")
    db.commit()
    with connect(_test_db) as other:
        first = queue.claim(db)
        second = queue.claim(other)
        third = queue.claim(other)
    assert first["kind"] == "import" and second["kind"] == "other" and third is None


def test_failed_job_is_requeued_with_backoff_then_fails(db):
    queue.enqueue(db, "import")
    db.commit()
    job = queue.claim(db)
    queue.fail(db, job, "boom")
    row = db.execute("SELECT status, run_after > now() AS later FROM job").fetchone()
    assert row == {"status": "queued", "later": True}
    db.execute("UPDATE job SET attempts = max_attempts, run_after = now()")
    db.commit()
    queue.fail(db, queue.claim(db), "boom")
    assert db.execute("SELECT status FROM job").fetchone()["status"] == "failed"
