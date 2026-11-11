"""Schema from nothing, and upgrade of an existing database: the lifecycle migration must repair what the old code left behind."""
import psycopg
import pytest

from tenderfit.db import MIGRATIONS, connect, migrate
from tenderfit.jobs import worker
from tenderfit.jobs.queue import pending


def _db(_test_db: str, name: str) -> str:
    admin = _test_db.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {name}")
    return _test_db.rsplit("/", 1)[0] + f"/{name}"


def test_fresh_database_gets_every_migration_once(_test_db):
    with connect(_db(_test_db, "tenderfit_mig_fresh")) as c:
        applied = migrate(c)
        assert applied == sorted(p.name for p in MIGRATIONS.glob("*.sql"))
        assert migrate(c) == [], "running again applies nothing"


def test_upgrade_repairs_what_a_failed_run_left_behind(_test_db):
    url = _db(_test_db, "tenderfit_mig_up")
    with connect(url) as c:
        c.execute("CREATE TABLE schema_migration (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < "011":
                c.execute(path.read_text())
                c.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
        c.execute("INSERT INTO raw_release (ocid, release_id, release_date, tags, payload, sha256, source_url) VALUES ('o', 'r', now(), '{}', '{}', 'x', 'u')")
        c.execute("INSERT INTO opportunity (ocid) VALUES ('o')")
        c.execute("""INSERT INTO notice_version (opportunity_id, ocid, release_id, raw_release_id, notice_type, release_tag, published_at, retrieved_at, content_hash, fields)
                     VALUES (1, 'o', 'r', 1, 'UK4', 'tender', now(), now(), 'x', '{}')""")
        c.execute("INSERT INTO lot (opportunity_id, lot_no) VALUES (1, '1')")
        c.execute("INSERT INTO business_profile (name) VALUES ('p')")
        # The old lifecycle: a good result superseded by a run that then crashed, with a null result still "current".
        c.execute("""INSERT INTO assessment (profile_id, lot_id, version_id, status, superseded, recommendation, dimensions)
                     VALUES (1, 1, 1, 'done', true, 'older', '{}'), (1, 1, 1, 'done', true, 'good', '{}'), (1, 1, 1, 'running', false, NULL, NULL)""")
        c.execute("INSERT INTO change_event (opportunity_id, kind, from_version_id, to_version_id, cls) VALUES (1, 'version', 1, 1, 'local')")
        c.execute("""INSERT INTO requirement (version_id, statement, category, obligation, quote) VALUES (1, 's', 'other', 'mandatory', 'q')""")
        c.commit()
        assert [p for p in migrate(c)] == sorted(p.name for p in MIGRATIONS.glob("*.sql") if p.name >= "011")
        rows = c.execute("SELECT recommendation, status, superseded, error FROM assessment ORDER BY id").fetchall()
        assert [(r["status"], r["superseded"]) for r in rows] == [("done", True), ("done", False), ("failed", True)]
        assert rows[1]["recommendation"] == "good", "the last good result is current again"
        assert "interrupted" in rows[2]["error"]
        assert c.execute("SELECT status FROM change_event").fetchone()["status"] == "applied", "earlier events were final"
        assert c.execute("SELECT name FROM business_profile").fetchone()["name"] == "p", "supplier data is preserved"
        assert c.execute("SELECT count(*) n FROM profile_member").fetchone()["n"] == 0, "nobody gains access to an existing supplier by upgrading"


def test_a_failing_migration_leaves_the_database_unchanged(_test_db, tmp_path, monkeypatch):
    url = _db(_test_db, "tenderfit_mig_fail")
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE should_vanish (id int);")
    (tmp_path / "002_bad.sql").write_text("SELECT this_is_not_valid_sql FROM nowhere;")
    monkeypatch.setattr("tenderfit.db.MIGRATIONS", tmp_path)
    with connect(url) as c:
        with pytest.raises(psycopg.Error):
            migrate(c)
        c.rollback()
        assert c.execute("SELECT to_regclass('should_vanish') r").fetchone()["r"] is None
        assert c.execute("SELECT to_regclass('schema_migration') r").fetchone()["r"] is None


def test_upgrade_from_the_previous_branch_state_keeps_documents_readable(_test_db):
    url = _db(_test_db, "tenderfit_mig_016")
    with connect(url) as c:
        c.execute("CREATE TABLE schema_migration (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < "017":
                c.execute(path.read_text())
                c.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
        c.execute("INSERT INTO raw_release (ocid, release_id, release_date, tags, payload, sha256, source_url) VALUES ('o', 'r', now(), '{}', '{}', 'x', 'u')")
        c.execute("INSERT INTO opportunity (ocid) VALUES ('o')")
        c.execute("""INSERT INTO notice_version (opportunity_id, ocid, release_id, raw_release_id, notice_type, release_tag, published_at, retrieved_at, content_hash, fields)
                     VALUES (1, 'o', 'r', 1, 'UK4', 'tender', now(), now(), 'x', '{}')""")
        c.execute("INSERT INTO document (version_id, origin, access, filename) VALUES (1, 'source', 'read', 'a.pdf')")
        c.execute("INSERT INTO document_ref (version_id, label, status, document_id) VALUES (1, 'A', 'read', 1)")
        c.commit()
        assert migrate(c) == sorted(p.name for p in MIGRATIONS.glob("*.sql") if p.name >= "017")
        d = c.execute("SELECT page_status, ocr_pages, access FROM document").fetchone()
        assert d == {"page_status": [], "ocr_pages": [], "access": "read"}, "documents read earlier keep their state until re-read"
        c.execute("UPDATE document_ref SET status = 'partial'")
        c.rollback()


def test_expected_extensions_constraints_and_indexes_exist(_test_db):
    url = _db(_test_db, "tenderfit_mig_schema")
    with connect(url) as c:
        migrate(c)
        assert c.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
        indexes = {r["indexname"] for r in c.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
        assert {"assessment_one_current", "assessment_one_running", "change_impact_once", "app_user_email", "passage_tsv"} <= indexes
        unvalidated = c.execute("SELECT conname FROM pg_constraint WHERE NOT convalidated").fetchall()
        assert unvalidated == []
        for bad in ("INSERT INTO app_user (email, password_hash) VALUES ('a@x.y', 'h'), ('A@X.Y', 'h')",):
            with pytest.raises(psycopg.errors.UniqueViolation):
                c.execute(bad)
            c.rollback()


def test_worker_schedules_unfinished_amendments_without_an_import(db, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr("tenderfit.changes.apply.pending_version_changes", lambda conn, respect_backoff=False: [(1, 2)])
    now = datetime.now(timezone.utc)
    assert worker.schedule_changes(db, now) and pending(db, "detect_changes")
    assert worker.schedule_changes(db, now) is None, "one at a time"


def test_import_can_be_switched_off(db, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr("tenderfit.jobs.worker.settings.import_enabled", False)
    assert worker.schedule_import(db, datetime.now(timezone.utc)) is None
