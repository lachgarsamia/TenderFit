"""Failure injection: whatever breaks during a new run, the last completed assessment stays readable."""
import pytest

from tenderfit.assessment import pipeline
from tenderfit.assessment.pipeline import assess_lot, recover_stale_assessments
from tenderfit.documents import service
from tenderfit.ingest.importer import run_import
from tenderfit.jobs import queue
from tenderfit.llm import LLMError
from tenderfit.profile.seed import seed
from tests import northfield
from tests.conftest import as_supplier
from tests.fakes import ScriptedLLM
from tests.test_importer import NOW, T0, T1, fake
from tests.test_pipeline import TODAY, portal, verdicts
from tests.test_retrieval import BagEmbedder, OverlapReranker


@pytest.fixture
def world(db, _test_db, tmp_path, monkeypatch):
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([northfield.release(deadline="2099-12-31T12:00:00Z")]), T0, T1, now=NOW)
    lot = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    kw = dict(today=TODAY, emb=BagEmbedder(), rr=OverlapReranker())
    good = assess_lot(db, lot, atlas, ScriptedLLM(verdicts), http=portal(northfield.pdf(northfield.SPEC_V1)), agent=False, **kw)
    return db, lot, atlas, kw, good


class Boom(RuntimeError):
    pass


def boom(*a, **k):
    raise Boom("injected failure")


class FailingEmbedder:
    def encode(self, texts):
        raise Boom("embedding failed")


def fresh_doc(db):
    """Give the run new work for the fetch, embed and extract stages to do."""
    vid = db.execute("SELECT current_version_id v FROM opportunity").fetchone()["v"]
    service.attach_upload(db, vid, "ITT.docx", northfield.itt_docx(), "sam")


def assert_previous_survives(db, lot, atlas, good, stage):
    with as_supplier(db, atlas) as c:
        a = c.get(f"/lots/{lot}/assessment").json()
    assert a["assessment_id"] == good and a["dimensions"] is not None
    assert a["processing"] is None and a["last_failure"]["stage"] == stage
    failed = db.execute("SELECT status, superseded, error, finished_at FROM assessment WHERE id > %s", (good,)).fetchone()
    assert failed["status"] == "failed" and failed["superseded"] and failed["error"] and failed["finished_at"]
    assert db.execute("SELECT count(*) n FROM assessment WHERE status = 'running'").fetchone()["n"] == 0


@pytest.mark.parametrize("stage,target", [("documents", "tenderfit.assessment.pipeline.docs.process_version"),
                                          ("extraction", "tenderfit.assessment.pipeline.extract_version"),
                                          ("assessment", "tenderfit.assessment.pipeline.decide"),
                                          ("finalising", "tenderfit.assessment.pipeline.agg.dimensions")])
def test_failure_at_each_stage_keeps_previous_assessment(world, monkeypatch, stage, target):
    db, lot, atlas, kw, good = world
    with monkeypatch.context() as m:
        m.setattr(target, boom)
        with pytest.raises(Boom):
            assess_lot(db, lot, atlas, ScriptedLLM(verdicts), **kw)
    assert_previous_survives(db, lot, atlas, good, stage)
    # A retry after the fault clears becomes current, and only one assessment is current.
    retry = assess_lot(db, lot, atlas, ScriptedLLM(verdicts), **kw)
    current = db.execute("SELECT id FROM assessment WHERE status = 'done' AND NOT superseded AND NOT shadow").fetchall()
    assert [r["id"] for r in current] == [retry]


def test_embedding_failure(world):
    db, lot, atlas, kw, good = world
    fresh_doc(db)
    with pytest.raises(Boom):
        assess_lot(db, lot, atlas, ScriptedLLM(verdicts), **{**kw, "emb": FailingEmbedder()})
    assert_previous_survives(db, lot, atlas, good, "embedding")


def test_extraction_llm_failure_is_recorded_not_hidden(world):
    db, lot, atlas, kw, good = world
    fresh_doc(db)
    def down(role, user, schema):
        raise LLMError("provider error 529: overloaded")
    with pytest.raises(LLMError):
        assess_lot(db, lot, atlas, ScriptedLLM(down), **kw)
    row = db.execute("SELECT error, error_stage FROM assessment WHERE id > %s", (good,)).fetchone()
    assert row["error_stage"] == "extraction" and "overloaded" in row["error"]
    assert_previous_survives(db, lot, atlas, good, "extraction")


@pytest.mark.agent
def test_agent_failure_keeps_previous_assessment(world, monkeypatch):
    db, lot, atlas, kw, good = world
    monkeypatch.setattr("tenderfit.agent.loop.run_agent", boom)
    with pytest.raises(Boom):
        assess_lot(db, lot, atlas, ScriptedLLM(verdicts), agent=True, **kw)
    assert_previous_survives(db, lot, atlas, good, "assessment")


def test_persistence_failure_rolls_back_cleanly(world, monkeypatch):
    db, lot, atlas, kw, good = world
    monkeypatch.setattr(pipeline, "_carry_verdict", boom)
    old = db.execute("SELECT requirement_id, id FROM verdict WHERE assessment_id = %s LIMIT 1", (good,)).fetchone()
    with pytest.raises(Boom):
        assess_lot(db, lot, atlas, ScriptedLLM(verdicts), carry={old["requirement_id"]: old["id"]}, **kw)
    assert_previous_survives(db, lot, atlas, good, "assessment")


def test_running_assessment_is_never_served_as_result(world):
    db, lot, atlas, kw, good = world
    db.execute("""INSERT INTO assessment (profile_id, lot_id, version_id, status, progress)
                  SELECT profile_id, lot_id, version_id, 'running', '{"stage": "assessment", "done": 2, "total": 5}'
                  FROM assessment WHERE id = %s""", (good,))
    db.commit()
    with as_supplier(db, atlas) as c:
        a = c.get(f"/lots/{lot}/assessment").json()
    assert a["assessment_id"] == good and a["dimensions"]["eligibility"]
    assert (a["processing"]["status"], a["processing"]["stage"], a["processing"]["done"]) == ("running", "assessment", 2)
    with pytest.raises(Exception):  # the database refuses a second running row
        db.execute("""INSERT INTO assessment (profile_id, lot_id, version_id, status)
                      SELECT profile_id, lot_id, version_id, 'running' FROM assessment WHERE id = %s""", (good,))
    db.rollback()


def test_first_run_failure_returns_status_not_a_crash(db, _test_db, tmp_path, monkeypatch):
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lot = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    with as_supplier(db, atlas) as c:
        assert c.get(f"/lots/{lot}/assessment").status_code == 404
    monkeypatch.setattr(pipeline.docs, "process_version", boom)
    with pytest.raises(Boom):
        assess_lot(db, lot, atlas, ScriptedLLM(verdicts), today=TODAY, emb=BagEmbedder(), rr=OverlapReranker())
    with as_supplier(db, atlas) as c:
        a = c.get(f"/lots/{lot}/assessment")
        assert a.status_code == 200 and a.json()["assessment_id"] is None and a.json()["last_failure"]["stage"] == "documents"
        assert c.get(f"/lots/{lot}/preparation").status_code == 404


def test_stale_running_row_is_recovered_and_new_attempt_closes_old_one(world):
    db, lot, atlas, kw, good = world
    db.execute("""INSERT INTO assessment (profile_id, lot_id, version_id, status, started_at)
                  SELECT profile_id, lot_id, version_id, 'running', now() - interval '3 hours' FROM assessment WHERE id = %s""", (good,))
    db.commit()
    assert recover_stale_assessments(db) == 1
    assert db.execute("SELECT count(*) n FROM assessment WHERE status = 'running'").fetchone()["n"] == 0
    assess_lot(db, lot, atlas, ScriptedLLM(verdicts), **kw)
    assert db.execute("SELECT count(*) n FROM assessment WHERE status = 'done' AND NOT superseded").fetchone()["n"] == 1


def test_duplicate_assess_clicks_share_one_waiting_job(world):
    db, lot, atlas, kw, good = world
    with as_supplier(db, atlas) as c:
        a = c.post(f"/lots/{lot}/assess", json={}).json()["jobs"]
        b = c.post(f"/lots/{lot}/assess", json={}).json()["jobs"]
        assert a == b
        state = c.get(f"/lots/{lot}/assessment").json()["processing"]
        assert state["status"] == "queued" and state["job_id"] == a[0]


def test_stale_job_with_no_attempts_left_fails_instead_of_looping(db):
    jid = queue.enqueue(db, "assess", {}, None)
    db.execute("""UPDATE job SET status = 'running', attempts = max_attempts, locked_at = now() - interval '2 hours'
                  WHERE id = %s""", (jid,))
    db.commit()
    assert queue.claim(db) is None
    assert db.execute("SELECT status FROM job WHERE id = %s", (jid,)).fetchone()["status"] == "failed"


