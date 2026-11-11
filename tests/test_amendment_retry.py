"""An amendment counts as applied only when its processing finished; retries are safe and leave no duplicates."""
import pytest

from tenderfit.assessment.pipeline import assess_lot
from tenderfit.changes import apply as changes
from tenderfit.changes.apply import ChangeProcessingError, pending_version_changes, process_pending
from tenderfit.documents import service
from tenderfit.ingest.importer import run_import
from tenderfit.llm import LLMError
from tenderfit.profile.seed import seed
from tests import northfield
from tests.conftest import as_supplier
from tests.fakes import ScriptedLLM
from tests.test_importer import NOW, T0, T1, fake
from tests.test_pipeline import TODAY, portal, verdicts
from tests.test_retrieval import BagEmbedder, OverlapReranker

KW = dict(today=TODAY, emb=BagEmbedder(), rr=OverlapReranker(), agent=False)
V1, V2 = northfield.pdf(northfield.SPEC_V1), northfield.pdf(northfield.SPEC_V2)
SPEC_V3 = {**northfield.SPEC_V2, 6: [("3.2 Delivery", "The supplier shall deliver all desks within 30 days of order. Delivery shall be made "
                                     "between 08:00 and 16:00 on Working Days as defined in Appendix B.")]}
V3 = northfield.pdf(SPEC_V3)


def amend(db, rid, day, url, deadline="2026-11-06T12:00:00Z"):
    r = northfield.release(rid=rid, tag="tenderUpdate", date=f"2026-10-{day}T18:00:00Z", deadline=deadline, spec_url=url)
    run_import(db, fake([r]), T0, T1, now=NOW)


@pytest.fixture
def world(db, _test_db, tmp_path, monkeypatch):
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    ids = seed(db)
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lots = {r["lot_no"]: r["id"] for r in db.execute("SELECT id, lot_no FROM lot")}
    llm = ScriptedLLM(verdicts)
    assess_lot(db, lots["1"], ids["Atlas Furniture"], llm, http=portal(V1), **KW)
    return db, lots, ids, llm


class FlakyExtraction(ScriptedLLM):
    """Fails on the first `failures` extraction calls."""

    def __init__(self, failures=1):
        super().__init__(verdicts)
        self.failures = failures

    def json(self, role, system, user, schema):
        if role == "extract" and self.failures:
            self.failures -= 1
            raise LLMError("provider error 529: overloaded")
        return super().json(role, system, user, schema)


def counts(db, vid):
    return db.execute("""SELECT statement, count(*) n FROM requirement WHERE version_id = %s GROUP BY statement""", (vid,)).fetchall()


def current(db, lot, profile):
    return db.execute("""SELECT * FROM assessment WHERE lot_id = %s AND profile_id = %s AND status = 'done' AND NOT superseded
                         AND NOT shadow""", (lot, profile)).fetchone()


def test_failed_extraction_is_retried_with_no_duplicates_and_no_loss(world):
    db, lots, ids, _ = world
    amend(db, "029400-2026", 12, "https://portal.test/Spec_Desks_v2.pdf")
    flaky = FlakyExtraction()
    before = current(db, lots["1"], ids["Atlas Furniture"])
    with pytest.raises(ChangeProcessingError, match="overloaded"):
        process_pending(db, flaky, http=portal(V2), **KW)

    ev = db.execute("SELECT * FROM change_event WHERE kind = 'version'").fetchone()
    assert (ev["status"], ev["attempts"]) == ("failed", 1) and "overloaded" in ev["last_error"]
    new_vid = ev["to_version_id"]
    assert counts(db, new_vid) == [], "nothing is carried when re-extraction fails"
    assert current(db, lots["1"], ids["Atlas Furniture"])["id"] == before["id"], "the old assessment stays current"
    assert pending_version_changes(db) == [(ev["from_version_id"], new_vid)], "a failed event is still pending"

    [impact] = process_pending(db, flaky, http=portal(V2), **KW)
    ev = db.execute("SELECT * FROM change_event WHERE kind = 'version'").fetchone()
    assert (ev["status"], ev["attempts"], ev["last_error"]) == ("applied", 2, None) and ev["finished_at"]
    rows = counts(db, new_vid)
    assert rows and all(r["n"] == 1 for r in rows), "no duplicate requirements"
    old = {r["statement"] for r in counts(db, ev["from_version_id"])}
    assert {s.replace("45", "30") for s in (r["statement"] for r in rows)} == old, "no lost requirements"
    assert pending_version_changes(db) == [] and process_pending(db, flaky, http=portal(V2), **KW) == []
    assert db.execute("SELECT count(*) n FROM change_impact WHERE id = %s", (impact,)).fetchone()["n"] == 1


def test_failure_while_preparing_carried_requirements_leaves_nothing(world, monkeypatch):
    db, lots, ids, llm = world
    amend(db, "029400-2026", 12, "https://portal.test/Spec_Desks_v2.pdf")
    real = changes.pair_requirements

    def boom(*a, **k):
        raise RuntimeError("crash after carrying requirements")
    monkeypatch.setattr(changes, "pair_requirements", boom)
    with pytest.raises(ChangeProcessingError):
        process_pending(db, llm, http=portal(V2), **KW)
    ev = db.execute("SELECT * FROM change_event WHERE kind = 'version'").fetchone()
    assert counts(db, ev["to_version_id"]) == []
    assert db.execute("SELECT count(*) n FROM version_preparation").fetchone()["n"] == 0
    monkeypatch.setattr(changes, "pair_requirements", real)
    process_pending(db, llm, http=portal(V2), **KW)
    assert db.execute("SELECT status FROM change_event WHERE kind = 'version'").fetchone()["status"] == "applied"
    assert all(r["n"] == 1 for r in counts(db, ev["to_version_id"]))


def test_exhausted_attempts_stop_retrying_until_a_person_asks(world):
    db, lots, ids, _ = world
    amend(db, "029400-2026", 12, "https://portal.test/Spec_Desks_v2.pdf")
    flaky = FlakyExtraction(failures=99)
    for _ in range(changes.MAX_EVENT_ATTEMPTS):
        with pytest.raises(ChangeProcessingError):
            process_pending(db, flaky, http=portal(V2), **KW)
    assert pending_version_changes(db) == []
    ev = db.execute("SELECT * FROM change_event WHERE kind = 'version'").fetchone()
    with as_supplier(db, ids["Atlas Furniture"]) as c:
        listed = c.get("/changes").json()
        assert listed[0]["status"] == "failed" and "overloaded" in listed[0]["last_error"]
        assert c.post(f"/changes/{ev['id']}/retry").status_code == 200
        assert c.post(f"/changes/{ev['id']}/retry").status_code == 409
    assert pending_version_changes(db)


def test_sequence_v1_v2_v3(world):
    db, lots, ids, llm = world
    amend(db, "029400-2026", 12, "https://portal.test/Spec_Desks_v2.pdf")
    process_pending(db, llm, http=portal(V2), **KW)
    amend(db, "029500-2026", 13, "https://portal.test/Spec_Desks_v3.pdf")
    process_pending(db, llm, http=portal(V3), **KW)
    rows = db.execute("SELECT status FROM change_event WHERE kind = 'version' ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["applied", "applied"]
    a = current(db, lots["1"], ids["Atlas Furniture"])
    vs = {r["statement"]: r["status"] for r in db.execute(
        "SELECT r.statement, v.status FROM verdict v JOIN requirement r ON r.id = v.requirement_id WHERE v.assessment_id = %s", (a["id"],))}
    assert vs["Deliver all desks within 30 days of order"] == "not_met", "v3 restored the original 30 days"
    with as_supplier(db, ids["Atlas Furniture"]) as c:
        listed = c.get("/changes").json()
    assert sorted((e["from_version_no"], e["to_version_no"]) for e in listed) == [(1, 2), (2, 3)]


def test_skipped_intermediate_version(world):
    db, lots, ids, llm = world
    amend(db, "029400-2026", 12, "https://portal.test/Spec_Desks_v2.pdf")
    amend(db, "029500-2026", 13, "https://portal.test/Spec_Desks_v3.pdf")
    [pair] = pending_version_changes(db)
    new = db.execute("SELECT current_version_id v FROM opportunity").fetchone()["v"]
    assert pair[1] == new
    process_pending(db, llm, http=portal(V3), **KW)
    assert current(db, lots["1"], ids["Atlas Furniture"])["version_id"] == new
    assert db.execute("SELECT count(*) n FROM change_event WHERE kind = 'version'").fetchone()["n"] == 1


def test_unfinished_event_is_superseded_by_a_newer_notice(world):
    db, lots, ids, _ = world
    amend(db, "029400-2026", 12, "https://portal.test/Spec_Desks_v2.pdf")
    with pytest.raises(ChangeProcessingError):
        process_pending(db, FlakyExtraction(), http=portal(V2), **KW)
    amend(db, "029500-2026", 13, "https://portal.test/Spec_Desks_v3.pdf")
    process_pending(db, ScriptedLLM(verdicts), http=portal(V3), **KW)
    assert [r["status"] for r in db.execute("SELECT status FROM change_event WHERE kind = 'version' ORDER BY id")] == ["superseded", "applied"]


def test_lots_and_profiles_succeed_or_fail_on_their_own(world, monkeypatch):
    db, lots, ids, llm = world
    pen = ids["Pennine Seating"]
    assess_lot(db, lots["2"], pen, llm, **KW)
    assess_lot(db, lots["1"], ids["Meridian Interiors"], llm, **KW)
    amend(db, "029400-2026", 12, "https://portal.test/Spec_Desks_v2.pdf")
    real = changes.assess_lot

    def fail_one(conn, lot, profile, *a, **k):
        if profile == pen and fail_one.on:
            raise RuntimeError("only Pennine fails")
        return real(conn, lot, profile, *a, **k)
    fail_one.on = True
    monkeypatch.setattr(changes, "assess_lot", fail_one)
    with pytest.raises(ChangeProcessingError, match="only Pennine"):
        process_pending(db, llm, http=portal(V2), **KW)
    ev = db.execute("SELECT * FROM change_event WHERE kind = 'version'").fetchone()
    assert ev["status"] == "failed"
    assert current(db, lots["1"], ids["Atlas Furniture"])["version_id"] == ev["to_version_id"]
    assert current(db, lots["1"], ids["Meridian Interiors"])["version_id"] == ev["to_version_id"]
    assert current(db, lots["2"], pen)["version_id"] == ev["from_version_id"]
    fail_one.on = False
    process_pending(db, llm, http=portal(V2), **KW)
    assert db.execute("SELECT status FROM change_event WHERE kind = 'version'").fetchone()["status"] == "applied"
    assert current(db, lots["2"], pen)["version_id"] == ev["to_version_id"]
    assert db.execute("SELECT count(*) n FROM change_impact").fetchone()["n"] == 3


def test_a_second_processor_leaves_the_work_to_the_first(world, _test_db):
    from tenderfit.db import connect
    db, lots, ids, llm = world
    amend(db, "029400-2026", 12, "https://portal.test/Spec_Desks_v2.pdf")
    with connect(_test_db) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (changes.AMENDMENT_LOCK,))
        other.commit()
        assert process_pending(db, llm, http=portal(V2), **KW) == [], "the lock holder does the work"
        assert db.execute("SELECT count(*) n FROM change_event").fetchone()["n"] == 0
        other.execute("SELECT pg_advisory_unlock(%s)", (changes.AMENDMENT_LOCK,))
        other.commit()
    assert len(process_pending(db, llm, http=portal(V2), **KW)) == 1


def test_automatic_retry_backs_off_but_a_direct_retry_does_not(world):
    db, lots, ids, _ = world
    amend(db, "029400-2026", 12, "https://portal.test/Spec_Desks_v2.pdf")
    with pytest.raises(ChangeProcessingError):
        process_pending(db, FlakyExtraction(), http=portal(V2), **KW)
    assert pending_version_changes(db, respect_backoff=True) == [], "just failed: wait"
    assert pending_version_changes(db), "a retry the queue or a person asks for goes ahead"
    db.execute("UPDATE change_event SET finished_at = now() - interval '3 minutes'")
    db.commit()
    assert pending_version_changes(db, respect_backoff=True), "after the pause it is picked up again"


def test_a_recheck_that_reached_the_new_version_first_neither_hides_nor_strands_the_amendment(world, monkeypatch):
    db, lots, ids, llm = world
    pid = ids["Atlas Furniture"]
    amend(db, "029400-2026", 12, "https://portal.test/Spec_Desks_v2.pdf")
    real = changes.pair_requirements
    monkeypatch.setattr(changes, "pair_requirements", lambda *a, **k: (_ for _ in ()).throw(LLMError("provider down")))
    with pytest.raises(ChangeProcessingError):
        process_pending(db, llm, http=portal(V2), **KW)
    monkeypatch.setattr(changes, "pair_requirements", real)
    assess_lot(db, lots["1"], pid, llm, http=portal(V2), **KW)  # a fact edit's job, running after the failure
    with as_supplier(db, pid) as c:
        ev = next(e for e in c.get("/changes").json() if e["kind"] == "version")
        assert ev["status"] == "failed"
        assert c.post(f"/changes/{ev['id']}/retry").status_code == 200
        process_pending(db, llm, http=portal(V2), **KW)
        assert next(e for e in c.get("/changes").json() if e["kind"] == "version")["status"] == "applied"
