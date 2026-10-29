import threading
from datetime import datetime, timezone

import httpx
import psycopg
import pytest

from tenderfit.db import connect
from tenderfit.ingest.client import FtsClient
from tenderfit.ingest.importer import replay, run_import
from tests.factories import uk4, uk6, uk12

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 2, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 2, 0, 30, tzinfo=timezone.utc)


def fake(*pages):
    def handler(req):
        i = int(req.url.params.get("cursor", 0))
        links = {"next": f"https://fts.test/x?cursor={i + 1}"} if i + 1 < len(pages) else {}
        return httpx.Response(200, json={"releases": pages[i], "links": links})
    return FtsClient(httpx.Client(transport=httpx.MockTransport(handler)), "https://fts.test", sleep=lambda s: None)


def counts(db):
    return db.execute("""SELECT (SELECT count(*) FROM raw_release) raw, (SELECT count(*) FROM notice_version) versions,
                                (SELECT count(*) FROM opportunity) opps, (SELECT count(*) FROM linked_notice) linked""").fetchone()


def test_import_is_idempotent_and_versions_by_release_id(db):
    pages = ([uk4(), uk4(rid="000150-2026", tag="tenderUpdate", date="2026-10-01T12:00:00Z", deadline="2026-11-06T12:00:00Z")],
             [{"ocid": "o-plan", "id": "p1", "date": "2026-10-01T10:00:00Z", "tag": ["planning"]}])
    first = run_import(db, fake(*pages), T0, T1, now=NOW)
    second = run_import(db, fake(*pages), T0, T1, now=NOW)
    assert first["new"] == 3 and second["new"] == 0
    assert counts(db) == {"raw": 3, "versions": 2, "opps": 1, "linked": 0}
    vs = db.execute("SELECT release_id, version_no FROM notice_version_numbered ORDER BY version_no").fetchall()
    assert [(v["release_id"], v["version_no"]) for v in vs] == [("000100-2026", 1), ("000150-2026", 2)]
    opp = db.execute("SELECT deadline, status FROM opportunity").fetchone()
    assert opp["deadline"] == datetime(2026, 11, 6, 12, tzinfo=timezone.utc) and opp["status"] == "open"


def test_out_of_order_update_does_not_regress_current_version(db):
    run_import(db, fake([uk4(rid="000150-2026", tag="tenderUpdate", date="2026-10-01T12:00:00Z", deadline="2026-11-06T12:00:00Z")]), T0, T1, now=NOW)
    run_import(db, fake([uk4()]), T0, T1, now=NOW)
    cur = db.execute("SELECT nv.release_id FROM opportunity o JOIN notice_version nv ON nv.id = o.current_version_id").fetchone()
    assert cur["release_id"] == "000150-2026"


def test_versions_and_raw_are_immutable(db):
    run_import(db, fake([uk4()]), T0, T1, now=NOW)
    for table in ("notice_version", "raw_release"):
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(f"UPDATE {table} SET ocid = 'x'")
        db.rollback()


def test_uk12_cancels_and_uk6_awards_per_lot(db):
    run_import(db, fake([uk4(lots=("1", "2")), uk6(lots=("1",), date="2026-10-01T15:00:00Z")]), T0, T1, now=NOW)
    lots = {r["lot_no"]: r["status"] for r in db.execute("SELECT lot_no, status FROM lot")}
    assert lots == {"1": "awarded", "2": "open"}
    run_import(db, fake([uk12(date="2026-10-01T16:00:00Z")]), T0, T1, now=NOW)
    assert db.execute("SELECT status FROM opportunity").fetchone()["status"] == "cancelled"


def test_failed_run_keeps_watermark_and_logs_error(db):
    def handler(req):
        return httpx.Response(500)
    broken = FtsClient(httpx.Client(transport=httpx.MockTransport(handler)), "https://fts.test", sleep=lambda s: None, max_retries=1)
    with pytest.raises(Exception):
        run_import(db, broken, T0, T1, now=NOW)
    assert db.execute("SELECT count(*) n FROM import_state").fetchone()["n"] == 0
    assert db.execute("SELECT error FROM import_run").fetchone()["error"]


def test_replay_rebuilds_same_state_from_raw(db):
    run_import(db, fake([uk4(), uk4(rid="000150-2026", tag="tenderUpdate", date="2026-10-01T12:00:00Z"), uk12(date="2026-10-01T16:00:00Z")]), T0, T1, now=NOW)
    before = counts(db), db.execute("SELECT ocid, status FROM opportunity").fetchall()
    replay(db, now=NOW)
    assert (counts(db), db.execute("SELECT ocid, status FROM opportunity").fetchall()) == before


def test_concurrent_imports_do_not_duplicate(db, _test_db):
    page = [uk4(), uk4(rid="000150-2026", tag="tenderUpdate", date="2026-10-01T12:00:00Z")]
    errors = []

    def go():
        try:
            with connect(_test_db) as c:
                run_import(c, fake(page), T0, T1, now=NOW)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert counts(db) == {"raw": 2, "versions": 2, "opps": 1, "linked": 0}
