"""Frozen declarations and aggregation-only overrides never mutate the live assessment."""
import pytest

from tenderfit.simulation import engine, scenarios, store
from tenderfit.simulation.changes import SimulationError
from tenderfit.simulation.snapshot import Snapshot
from tests.test_isolation import world  # noqa: F401
from tests.test_pipeline import TODAY
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint
from tests.test_simulation_engine import by_requirement, simulate, status
from tests.test_simulation_inputs import override

base = _base


def answer(b, text, profile=None):
    db = b["db"]
    verdict = db.execute("SELECT id FROM verdict WHERE assessment_id = %s AND requirement_id = %s",
                         (b["a"], b["standard"])).fetchone()["id"]
    db.execute("""INSERT INTO question (verdict_id, text, answer, answered_at, answered_by, requirement_id, profile_id)
                  VALUES (%s, 'Tested?', %s, now(), 'tester', %s, %s)""",
               (verdict, text, b["standard"], profile or b["atlas"]))
    db.commit()


def change(b, text):
    return {"kind": "set_requirement_answer", "requirement_id": b["standard"], "answer": text}


def test_new_answers_are_declarations_and_leave_live_state_untouched(base):
    before = live_fingerprint(base["db"])
    sid = simulate(base, [change(base, " Yes, tested ")])
    out = engine.run(base["db"], base["atlas"], sid)
    row = by_requirement(base, sid)[base["standard"]]
    assert (row["control_status"], row["simulated_status"]) == ("unknown", "needs_confirmation")
    assert out["summary"]["changed"][0]["requirement_id"] == base["standard"]
    assert live_fingerprint(base["db"]) == before
    assert status(base, sid)["engine_version"] == "sim-v3"


def test_answers_and_overrides_are_frozen_across_live_edits_and_variations(base, monkeypatch):
    answer(base, "First")
    answer(base, "Latest")
    answer(base, "Other supplier", base["pennine"])
    override(base["db"], base["atlas"], base["lead"])
    sid = simulate(base, [])
    stored = status(base, sid)
    snap = Snapshot.from_json(stored["snapshot"])
    assert snap.answers[base["standard"]] == ["First", "Latest"]
    assert snap.overridden[base["lead"]]
    assert snap.sha256() == stored["snapshot_sha256"]
    assert Snapshot.from_json(snap.to_json()).to_json() == snap.to_json()
    base["db"].execute("DELETE FROM question")
    base["db"].execute("DELETE FROM confirmation WHERE point = 'G6'")
    base["db"].commit()
    child = store.create_variation(base["db"], base["atlas"], sid, "tester", changes=[change(base, None)])
    assert status(base, child)["snapshot_sha256"] == stored["snapshot_sha256"]
    def forbidden(*args):
        raise AssertionError("v3 must not read live overrides")
    monkeypatch.setattr(engine, "overrides", forbidden)
    out = engine.run(base["db"], base["atlas"], child)
    row = by_requirement(base, child)[base["standard"]]
    assert (row["control_status"], row["simulated_status"]) == ("needs_confirmation", "unknown")
    assert not out["summary"]["recommendation"]["control"].startswith("Blocker")
    assert "answers" not in out["summary"]["execution"]["live_inputs"]
    assert out["summary"]["input_drift"]["since_creation"] == []


def test_answer_after_creation_cannot_change_control(base):
    sid = simulate(base, [])
    answer(base, "Yes")
    override(base["db"], base["atlas"], base["lead"])
    out = engine.run(base["db"], base["atlas"], sid)
    assert by_requirement(base, sid)[base["standard"]]["control_status"] == "unknown"
    assert out["summary"]["recommendation"]["control"].startswith("Blocker")
    assert out["summary"]["input_drift"] == {"since_creation": [], "since_baseline": []}


@pytest.mark.parametrize("initial", [False, True])
def test_override_changes_recommendation_but_never_verdict(base, initial):
    if initial:
        override(base["db"], base["atlas"], base["lead"])
    before = live_fingerprint(base["db"])
    sid = simulate(base, [{"kind": "set_override", "requirement_id": base["lead"], "on": not initial}])
    out = engine.run(base["db"], base["atlas"], sid)
    row = by_requirement(base, sid)[base["lead"]]
    assert row["control_status"] == row["simulated_status"] == "not_met"
    s = out["summary"]
    assert s["recommendation"]["control"].startswith("Blocker") is not initial
    assert s["recommendation"]["simulated"].startswith("Blocker") is initial
    assert s["changed"] == []
    assert s["override_changes"][0]["before"] is initial
    assert s["override_changes"][0]["after"] is not initial
    assert live_fingerprint(base["db"]) == before


def test_declaration_cannot_clear_a_failed_rule_or_guess_model_result(base):
    sid = simulate(base, [{"kind": "set_requirement_answer", "requirement_id": base[r], "answer": "Yes"}
                          for r in ("lead", "install")])
    engine.run(base["db"], base["atlas"], sid)
    rows = by_requirement(base, sid)
    assert rows[base["lead"]]["simulated_status"] == "not_met"
    assert rows[base["install"]]["how"] == "needs_model"


@pytest.mark.parametrize("value", ["", "   ", "x" * 2001, 123])
def test_invalid_answer_rejected_atomically(base, value):
    before = base["db"].execute("SELECT count(*) n FROM simulation").fetchone()["n"]
    base["db"].rollback()
    with pytest.raises(SimulationError):
        store.create(base["db"], base["atlas"], base["a"], "tester", changes=[change(base, value)])
    assert base["db"].execute("SELECT count(*) n FROM simulation").fetchone()["n"] == before
    base["db"].rollback()


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_snapshots_retain_hash_and_reject_new_changes(base, monkeypatch, version):
    original = store.take_snapshot
    def legacy(*args, **kwargs):
        snap, drift = original(*args, **kwargs)
        snap.answers = snap.overridden = snap.decision_drift = None
        if version == 1:
            snap.support = None
        return snap, drift
    monkeypatch.setattr(store, "take_snapshot", legacy)
    monkeypatch.setattr(store, "ENGINE_VERSION", f"sim-v{version}")
    sid = simulate(base, [])
    parent = status(base, sid)
    snap = Snapshot.from_json(parent["snapshot"])
    assert snap.to_json() == parent["snapshot"] and snap.sha256() == parent["snapshot_sha256"]
    assert not scenarios.view(snap)["decision_inputs_frozen"]
    with pytest.raises(SimulationError):
        store.add_change(base["db"], base["atlas"], sid, change(base, "Yes"))
    child = store.create_variation(base["db"], base["atlas"], sid, "tester")
    assert status(base, child)["engine_version"] == f"sim-v{version}"
    answer(base, "Yes")
    engine.run(base["db"], base["atlas"], child)
    assert by_requirement(base, child)[base["standard"]]["control_status"] == "needs_confirmation"


@pytest.mark.parametrize("on", [1, "true", None])
def test_override_requires_a_boolean(base, on):
    with pytest.raises(SimulationError):
        simulate(base, [{"kind": "set_override", "requirement_id": base["lead"], "on": on}])


def test_unknown_requirement_is_rejected(base):
    with pytest.raises(SimulationError):
        simulate(base, [{"kind": "set_requirement_answer", "requirement_id": 999999, "answer": "Yes"}])


def test_v3_evaluation_never_reads_live_decisions(base, monkeypatch):
    import re

    import psycopg
    sid = simulate(base, [change(base, "Yes")])
    original = psycopg.Cursor.execute
    def execute(self, query, *args, **kwargs):
        assert not re.search(r"\b(?:FROM|JOIN)\s+(?:question|confirmation)\b", str(query), re.I)
        return original(self, query, *args, **kwargs)
    monkeypatch.setattr(psycopg.Cursor, "execute", execute)
    engine.run(base["db"], base["atlas"], sid, selective=False)
    row = by_requirement(base, sid)[base["standard"]]
    assert row["simulated"]["simulated"]["provenance"]["decision_inputs"] == {
        "answers": ["Yes"], "overridden": False, "origin": "modified"}


def test_answer_inputs_and_results_stay_supplier_scoped(base):
    from tests.conftest import as_supplier
    a, b = as_supplier(base["db"], base["atlas"]), as_supplier(base["db"], base["pennine"])
    before = live_fingerprint(base["db"])
    created = a.post("/simulations", json={"assessment_id": base["a"], "as_of": TODAY.isoformat(),
                                           "changes": [change(base, "Private declaration")]})
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    values = a.get(f"/simulations/{sid}/inputs").json()
    assert values["decision_inputs_frozen"]
    assert a.post(f"/simulations/{sid}/run").status_code == 200
    for path in (f"/simulations/{sid}/inputs", f"/simulations/{sid}/results/{base['standard']}/provenance"):
        assert b.get(path).status_code == 404
    assert b.post(f"/simulations/{sid}/changes", json={"change": change(base, "Overwrite")}).status_code == 404
    assert live_fingerprint(base["db"]) == before
