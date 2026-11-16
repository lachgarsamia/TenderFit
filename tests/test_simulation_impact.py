"""Dependency-aware simulation: only the requirements a hypothesis can reach are recomputed.

The central guarantee is that selective recomputation gives exactly what full recomputation gives. A full run
(selective=False) recomputes everything and refuses to complete if a requirement it was told nothing reaches came out
different, so every full run below is also a check of the dependency analysis."""
import pytest

from tenderfit.changes.impact import Dep, load_deps
from tenderfit.rules.engine import Fact
from tenderfit.simulation import engine, impact, store
from tenderfit.simulation.changes import SimulationError
from tests.test_isolation import world  # noqa: F401  (fixture)
from tests.test_pipeline import TODAY
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint, valid_changes

base = _base  # the fixture, shared with test_simulation.py

NEW_TEST_REPORT = {"kind": "add_fact", "attribute": "test_report", "label": "BS EN 1729-2 test report (2026)",
                   "value": {"product": "ND-120", "standard": "BS EN 1729-2"}, "evidence_status": "document_on_file"}


def simulate(b, changes) -> int:
    sid = store.create(b["db"], b["atlas"], b["a"], "tester", as_of=TODAY)
    for c in changes:
        store.add_change(b["db"], b["atlas"], sid, c)
    return sid


def results(b, sid) -> dict[int, dict]:
    rows = {r["requirement_id"]: {k: v for k, v in r.items() if k != "simulation_id"} for r in store.results(b["db"], b["atlas"], sid)}
    b["db"].rollback()
    return rows


def hypotheses(b) -> list[list[dict]]:
    f = b["facts"]
    single = [[c] for c in valid_changes(b) if c.get("fact_id") != -1]  # that one edits a fact an earlier change adds
    return single + [
        [NEW_TEST_REPORT],
        [{"kind": "remove_fact", "fact_id": f["capacity"]}],
        [{"kind": "set_fact_value", "fact_id": f["capacity"], "key": "max_units", "value": 150}],
        [{"kind": "set_notice_field", "field": "lot.quantity", "value": 250}],
        [{"kind": "set_fact_evidence", "fact_id": 10, "evidence_status": "document_on_file"}],
        [{"kind": "set_notice_field", "field": "notice.deadline", "value": "2026-11-17"}],
        valid_changes(b),
    ]


# --- Selective == full -----------------------------------------------------------------------------------------------------

def test_selective_recomputation_gives_exactly_the_full_result(base):
    db = base["db"]
    before = live_fingerprint(db)
    for n, changes in enumerate(hypotheses(base)):
        sel, full = simulate(base, changes), simulate(base, changes)
        s = engine.run(db, base["atlas"], sel)
        f = engine.run(db, base["atlas"], full, selective=False)
        assert results(base, sel) == results(base, full), f"hypothesis {n}: {changes}"
        assert s["summary"] == f["summary"], f"hypothesis {n}"
    assert live_fingerprint(db) == before


def test_a_requirement_nothing_reaches_is_not_recomputed(base, monkeypatch):
    db = base["db"]
    sid = simulate(base, [{"kind": "set_condition_bound", "requirement_id": base["lead"], "condition": 0, "bound": 45}])
    seen = []
    real = engine._decide
    monkeypatch.setattr(engine, "_decide", lambda conn, snap, s, live, config: seen.append((s is not None, live["id"])) or
                        real(conn, snap, s, live, config))
    engine.run(db, base["atlas"], sid)
    computed = [rid for _, rid in seen]
    assert computed.count(base["lead"]) == 2, "control and simulated"
    assert computed.count(base["quantity"]) == computed.count(base["standard"]) == 1, "control only"
    rows = results(base, sid)
    assert rows[base["quantity"]]["how"] == "not_affected"
    assert rows[base["quantity"]]["simulated_status"] == rows[base["quantity"]]["control_status"]


# --- Why recorded dependencies alone are not enough -------------------------------------------------------------------------

def test_a_new_fact_reaches_a_requirement_no_recorded_dependency_points_to(base):
    """The BS EN 1729-2 verdict recorded only the test report it found (evidence missing). A new test report changes it, and
    no recorded dependency could say so: the reach comes from what the condition examines."""
    db = base["db"]
    deps = load_deps(db, base["a"])
    db.rollback()
    assert {d.target_id for d in deps if d.requirement_id == base["standard"] and d.target_type == "fact"} == {"10"}
    sid = simulate(base, [NEW_TEST_REPORT])
    engine.run(db, base["atlas"], sid)
    standard = results(base, sid)[base["standard"]]
    assert [(x["kind"], x["target"]) for x in standard["simulated"]["reached"]] == [("examines", "fact:-1")]
    assert standard["control_status"] == "unknown" and standard["simulated_status"] != "unknown"
    assert standard["simulated"]["effect"]


def test_a_full_run_refuses_to_complete_when_the_analysis_misses_a_requirement(base, monkeypatch):
    """With recorded dependencies only, the new test report would be missed. The full run catches it instead of
    reporting a wrong 'not affected'."""
    db = base["db"]
    monkeypatch.setattr(engine, "reach", lambda reqs, touched, deps, facts: impact.reach(
        reqs, {t for t in touched if (t[0], t[1]) in {(d.target_type, d.target_id) for d in deps}}, deps, []))
    sid = simulate(base, [NEW_TEST_REPORT])
    with pytest.raises(SimulationError, match="dependency analysis is incomplete"):
        engine.run(db, base["atlas"], sid, selective=False)
    row = store.get(db, base["atlas"], sid)
    db.rollback()
    assert (row["status"], row["error_stage"]) == ("failed", "evaluation")


# --- Explaining impact ------------------------------------------------------------------------------------------------------

def test_preview_explains_the_reach_before_running_and_writes_nothing(base):
    db = base["db"]
    capacity = base["facts"]["capacity"]
    sid = simulate(base, [{"kind": "set_fact_value", "fact_id": capacity, "key": "max_units", "value": 150}])
    before = live_fingerprint(db)
    reached = engine.preview(db, base["atlas"], sid)
    assert live_fingerprint(db) == before
    assert [x["kind"] for x in reached[base["quantity"]]] == ["used"]
    assert "Up to 300 desks" in reached[base["quantity"]][0]["detail"]
    assert reached[base["install"]][0]["kind"] == "examines", "the model reads every fact for a requirement of category other"
    assert reached[base["standard"]] == []
    engine.run(db, base["atlas"], sid)
    assert {rid: r["simulated"]["reached"] for rid, r in results(base, sid).items()} == reached
    assert store.get(db, base["atlas"], sid)["status"] == "completed"
    db.rollback()


def test_a_live_edit_since_the_baseline_flags_model_verdicts_that_may_be_out_of_date(base):
    db = base["db"]
    db.execute("UPDATE profile_fact SET label = 'Public liability (renewed)', updated_at = now() WHERE id = %s",
               (base["facts"]["insurance"],))
    db.commit()
    sid = simulate(base, [{"kind": "set_condition_bound", "requirement_id": base["lead"], "condition": 0, "bound": 45}])
    out = engine.run(db, base["atlas"], sid)
    install = results(base, sid)[base["install"]]
    assert install["how"] == "not_affected"
    assert install["simulated"]["notes"][0].startswith("baseline may be out of date")
    assert out["summary"]["stale_baseline"] == [base["install"]]


# --- The analysis itself (pure) ---------------------------------------------------------------------------------------------

def _req(rid, category, *conditions):
    return {"id": rid, "category": category, "conditions": [{"condition": c} for c in conditions]}


LEAD = _req(1, "delivery", {"kind": "at_most", "fact": "capacity.lead_days", "bound": 30})
QTY = _req(2, "quantity", {"kind": "at_least", "fact": "capacity.max_units", "bound": "lot.quantity"})
MODEL = _req(3, "insurance", None)
FACTS = [Fact(6, "capacity", "Capacity", {}, "declared"), Fact(12, "insurance", "PL", {}, "document_on_file")]


def test_what_each_requirement_reads():
    assert impact.reads(LEAD) == ({"capacity"}, {"notice.deadline"}, False)
    assert impact.reads(QTY) == ({"capacity"}, {"notice.deadline", "lot.quantity"}, False)
    assert impact.reads(MODEL) == ({"insurance"}, {"notice.deadline"}, True)
    assert impact.reads(_req(4, "other", None))[0] == {"*"}
    assert "lot.regions" in impact.reads(_req(5, "delivery", {"kind": "region_covered", "fact": "region"}))[1]


def test_reach_by_field_fact_and_direct_change():
    r = impact.reach([LEAD, QTY, MODEL], {("field", "lot.quantity")}, [], FACTS)
    assert (bool(r[1]), bool(r[2]), bool(r[3])) == (False, True, False)
    r = impact.reach([LEAD, QTY, MODEL], {("field", "notice.deadline")}, [], FACTS)
    assert all(r.values()), "validity is measured at the deadline, and the model is told it"
    r = impact.reach([LEAD, QTY, MODEL], {("fact", "12")}, [], FACTS)
    assert (bool(r[1]), bool(r[2]), bool(r[3])) == (False, False, True)
    r = impact.reach([LEAD, QTY, MODEL], {("requirement", "1")}, [], FACTS)
    assert [x.kind for x in r[1]] == ["direct"] and not r[2]


def test_a_recorded_dependency_is_reported_as_used_rather_than_examined():
    deps = [Dep(1, "fact", "fact", "6")]
    r = impact.reach([LEAD, QTY], {("fact", "6")}, deps, FACTS)
    assert [x.kind for x in r[1]] == ["used"] and [x.kind for x in r[2]] == ["examines"]
