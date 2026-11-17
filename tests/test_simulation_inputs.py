"""Live inputs and their drift (review finding M2).

The snapshot freezes facts, requirement wording and conditions, tender fields and the date, and its hash covers only
that. A run also reads overrides, answers, live requirement rows, the document pack, rejections and the baseline record.
These tests check that every such read is tracked, that control and simulated see one consistent state of it, and that
a change between creation and run, or since the baseline, is reported as drift even when no requirement's status moves."""
import re
import threading

import psycopg
from psycopg.types.json import Jsonb

from tenderfit.db import connect
from tenderfit.simulation import engine, inputs, store
from tests.test_isolation import world  # noqa: F401  (fixture)
from tests.test_pipeline import TODAY
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint, valid_changes
from tests.test_simulation_api import api as _api
from tests.test_simulation_api import lead, new

base, api = _base, _api  # fixtures, shared with test_simulation.py and test_simulation_api.py

TABLE = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_]+)", re.I)


def sim(api) -> dict:
    """On the date the fixture's baseline was decided, so the only drift is what a test introduces."""
    return new(api, as_of=TODAY.isoformat())


def override(db, profile_id: int, requirement_id: int) -> None:
    """What the supplier's "override this not-met verdict" decision records (point G6)."""
    db.execute("""INSERT INTO confirmation (point, user_name, target_type, target_id, decision, reason, profile_id, payload)
                  VALUES ('G6', 'tester', 'requirement', %s, 'override', 'we can meet it', %s, %s)""",
               (requirement_id, profile_id, Jsonb({"requirement_id": requirement_id})))
    db.commit()


def summary(api, sid) -> dict:
    r = api["A"].post(f"/simulations/{sid}/run")
    assert r.status_code == 200, r.text
    return r.json()["summary"]


def rows(api, sid) -> dict[int, dict]:
    return {r["requirement_id"]: r for r in api["A"].get(f"/simulations/{sid}/results").json()["results"]}


# --- The finding, reproduced --------------------------------------------------------------------------------------------

def test_an_override_recorded_after_creation_is_reported_although_no_status_moves(api):
    """Before the fix the control recommendation changed with nothing in summary.drift to say why."""
    db = api["db"]
    created = sim(api)
    override(db, api["atlas"], api["lead"])
    before = live_fingerprint(db)
    s = summary(api, created["id"])

    assert s["recommendation"]["baseline"].startswith("Blocker found") and not s["recommendation"]["control"].startswith("Blocker")
    assert s["recommendation"]["drift"] is True
    assert s["drift"] == [], "no requirement's own status moved"
    assert s["input_drift"]["since_creation"] == [
        {"input": "overrides", "requirement_id": api["lead"], "detail": inputs.DETAIL["overrides"]}]
    live = s["execution"]["live_inputs"]
    assert live["overrides"]["changed"] and not any(v["changed"] for k, v in live.items() if k != "overrides")
    assert s["execution"]["snapshot_sha256"] == created["snapshot_sha256"], "the snapshot hash keeps its meaning"
    assert "overrides" not in s["execution"]["frozen"] and "supplier facts" in s["execution"]["frozen"]

    lead_row = rows(api, created["id"])[api["lead"]]
    assert lead_row["baseline_status"] == lead_row["control_status"] == "not_met"
    assert [e["input"] for e in lead_row["input_drift"]] == ["overrides", "overrides"], "since creation, and since the baseline"
    assert any("since this simulation was created" in n for n in lead_row["notes"])
    assert live_fingerprint(db) == before


def test_an_override_recorded_before_creation_is_drift_since_the_baseline(api):
    override(api["db"], api["atlas"], api["lead"])
    s = summary(api, sim(api)["id"])
    assert s["input_drift"]["since_creation"] == []
    assert s["input_drift"]["since_baseline"] == [
        {"input": "overrides", "requirement_id": api["lead"], "detail": "an override was recorded after the baseline ran"}]
    assert s["recommendation"]["drift"] is True


def test_a_document_change_after_creation_is_lot_level_drift(api):
    db = api["db"]
    sid = sim(api)["id"]
    db.execute("UPDATE document_ref SET note = 'set aside by the supplier' WHERE id = (SELECT min(id) FROM document_ref)")
    db.commit()
    s = summary(api, sid)
    assert s["input_drift"]["since_creation"] == [{"input": "documents", "requirement_id": None, "detail": inputs.DETAIL["documents"]}]
    assert s["execution"]["live_inputs"]["documents"]["changed"]
    assert all(any(e["input"] == "documents" for e in r["input_drift"]) for r in rows(api, sid).values())


def test_an_answer_recorded_after_creation_is_reported_for_its_requirement(api):
    db = api["db"]
    sid = sim(api)["id"]
    verdict = db.execute("SELECT id FROM verdict WHERE assessment_id = %s AND requirement_id = %s",
                         (api["a"], api["standard"])).fetchone()["id"]
    db.execute("""INSERT INTO question (verdict_id, text, answer, answered_at, answered_by, requirement_id, profile_id)
                  VALUES (%s, 'Do the desks meet BS EN 1729-2?', 'Yes, tested in 2026', now(), 'tester', %s, %s)""",
               (verdict, api["standard"], api["atlas"]))
    db.commit()
    s = summary(api, sid)
    assert {(e["input"], e["requirement_id"]) for e in s["input_drift"]["since_creation"]} == {("answers", api["standard"])}
    assert ("answers", api["standard"]) in {(e["input"], e["requirement_id"]) for e in s["input_drift"]["since_baseline"]}


def test_nothing_changed_means_no_input_drift(api):
    s = summary(api, sim(api)["id"])
    assert s["input_drift"] == {"since_creation": [], "since_baseline": []}
    assert not any(v["changed"] for v in s["execution"]["live_inputs"].values())
    assert s["recommendation"]["drift"] is False


def test_a_simulation_created_before_inputs_were_recorded_says_so(api):
    db = api["db"]
    old = sim(api)["id"]
    sid = db.execute("""INSERT INTO simulation (profile_id, lot_id, baseline_assessment_id, version_id, title, as_of, snapshot,
                                                snapshot_sha256, drift, engine_version, created_by)
                        SELECT profile_id, lot_id, baseline_assessment_id, version_id, title, as_of, snapshot, snapshot_sha256,
                               drift - 'live_inputs_at_creation', engine_version, created_by FROM simulation WHERE id = %s
                        RETURNING id""", (old,)).fetchone()["id"]
    db.commit()
    s = summary(api, sid)
    assert [e["input"] for e in s["input_drift"]["since_creation"]] == ["all"]
    assert all(v["at_creation"] is None for v in s["execution"]["live_inputs"].values())


def test_the_recorded_inputs_are_not_shown_as_drift_and_cannot_be_rewritten(api):
    db = api["db"]
    created = sim(api)
    assert "live_inputs_at_creation" not in created["drift"] and "facts_changed_since_baseline" in created["drift"]
    try:
        db.execute("UPDATE simulation SET drift = '{}' WHERE id = %s", (created["id"],))
        raise AssertionError("the creation record was rewritten")
    except psycopg.errors.RaiseException:
        db.rollback()


# --- Control and simulated see the same inputs ----------------------------------------------------------------------------

def test_control_and_simulated_see_one_state_of_the_live_inputs(base, _test_db, monkeypatch):
    """An answer committed by someone else between the control and simulated decisions of the same requirement. Under
    READ COMMITTED the simulated decision would see it and the control would not: a false effect, which a full run
    refuses as "dependency analysis is incomplete". One REPEATABLE READ transaction makes both see the same state."""
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    verdict = db.execute("SELECT id FROM verdict WHERE assessment_id = %s AND requirement_id = %s",
                         (base["a"], base["standard"])).fetchone()["id"]
    db.rollback()
    real, seen = engine._decide, []

    def decide(conn, snap, s, live, config):
        out = real(conn, snap, s, live, config)
        seen.append((live["id"], out and out["status"]))
        if live["id"] == base["standard"] and len([x for x in seen if x[0] == base["standard"]]) == 1:
            with connect(_test_db) as other:  # another request, committing between the two decisions
                other.execute("""INSERT INTO question (verdict_id, text, answer, answered_at, answered_by, requirement_id, profile_id)
                                 VALUES (%s, 'Tested?', 'Yes', now(), 'someone', %s, %s)""", (verdict, base["standard"], base["atlas"]))
        return out
    monkeypatch.setattr(engine, "_decide", decide)
    out = engine.run(db, base["atlas"], sid, selective=False)
    assert out["status"] == "completed"
    standard = [status for rid, status in seen if rid == base["standard"]]
    assert len(standard) == 2 and standard[0] == standard[1] == "unknown"
    assert out["summary"]["input_drift"]["since_creation"] == [], "the provenance is the state the decisions used"


# --- Every live read is tracked -------------------------------------------------------------------------------------------

def test_every_table_a_run_reads_is_frozen_or_tracked(base, monkeypatch):
    """A run reads no live table that isn't one of the tracked inputs: a new live read added to the engine or the reused
    assessment code fails here until it is tracked (and so reported when it changes)."""
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    for c in valid_changes(base):
        store.add_change(db, base["atlas"], sid, c)
    reads: set[str] = set()
    real_execute, real_evaluate = psycopg.Cursor.execute, engine._evaluate
    active = threading.Event()

    def spy(self, query, params=None, **kw):
        if active.is_set():
            reads.update(t.lower() for t in TABLE.findall(str(query)))
        return real_execute(self, query, params, **kw)

    def evaluate(*args, **kwargs):
        active.set()
        try:
            return real_evaluate(*args, **kwargs)
        finally:
            active.clear()
    monkeypatch.setattr(psycopg.Cursor, "execute", spy)
    monkeypatch.setattr(engine, "_evaluate", evaluate)
    engine.run(db, base["atlas"], sid, selective=False)
    tracked = {t for tables in inputs.TABLES.values() for t in tables}
    live_reads = {t for t in reads if not t.startswith("simulation")}
    assert live_reads, "the spy saw the run's queries"
    assert live_reads <= tracked, f"read live but not tracked: {sorted(live_reads - tracked)}"
    assert {"confirmation", "question", "requirement", "document_ref"} <= live_reads


def test_the_live_input_digests_ignore_the_inventorys_timestamp(base):
    """inventory() stamps the time it was read; two reads of an unchanged database must give the same digests."""
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    out = engine.run(db, base["atlas"], sid)
    assert not any(v["changed"] for v in out["summary"]["execution"]["live_inputs"].values())


def test_inputs_are_scoped_to_the_supplier(api):
    """Another supplier's overrides never show up as this supplier's drift."""
    sid = sim(api)["id"]
    override(api["db"], api["pennine"], api["lead"])
    s = summary(api, sid)
    assert s["input_drift"] == {"since_creation": [], "since_baseline": []}


def test_a_hypothesis_with_input_drift_still_reports_its_own_effect(api):
    db = api["db"]
    sid = sim(api)["id"]
    api["A"].post(f"/simulations/{sid}/changes", json={"change": lead(api)})
    db.execute("UPDATE document_ref SET note = 'changed' WHERE id = (SELECT min(id) FROM document_ref)")
    db.commit()
    s = summary(api, sid)
    assert [c["requirement_id"] for c in s["changed"]] == [api["lead"]], "the effect is control -> simulated, as before"
    assert [e["input"] for e in s["input_drift"]["since_creation"]] == ["documents"]
