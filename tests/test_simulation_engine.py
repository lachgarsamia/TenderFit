"""Running simulations: the live verdict rules, on a frozen snapshot, with and without the hypothesis.

Every test that runs a simulation compares live_fingerprint before and after: a run writes only its own simulation's rows,
whether it succeeds or fails."""
import psycopg
import pytest
from psycopg.types.json import Jsonb

from tenderfit.assessment.pipeline import decide, load_facts, lot_context
from tenderfit.demo.__main__ import reset
from tenderfit.rules.engine import NOT_MET
from tenderfit.simulation import engine, store
from tenderfit.simulation.changes import SimulationError, SimulationNotFound
from tests.test_demo_and_api import ARGS
from tests.test_demo_and_api import demo as _demo
from tests.test_isolation import world  # noqa: F401  (fixture)
from tests.test_pipeline import TODAY
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint, valid_changes

base = _base  # the fixture, shared with test_simulation.py
demo = _demo

LEAD_OK = {"kind": "set_condition_bound", "condition": 0, "bound": 45}


def simulate(b, changes, title="question") -> int:
    sid = store.create(b["db"], b["atlas"], b["a"], "tester", title, as_of=TODAY)
    for c in changes:
        store.add_change(b["db"], b["atlas"], sid, c)
    return sid


def by_requirement(b, sid) -> dict[int, dict]:
    rows = {r["requirement_id"]: r for r in store.results(b["db"], b["atlas"], sid)}
    b["db"].rollback()
    return rows


def status(b, sid) -> dict:
    row = store.get(b["db"], b["atlas"], sid)
    b["db"].rollback()
    return row


# --- A run computes the hypothesis's effect and writes nothing live -------------------------------------------------------

def test_a_run_reports_the_effect_and_changes_nothing_live(base):
    db = base["db"]
    sid = simulate(base, [{**LEAD_OK, "requirement_id": base["lead"]}], "what if 45 days were accepted?")
    before, calls = live_fingerprint(db), len(base["llm"].users)
    out = engine.run(db, base["atlas"], sid)
    assert live_fingerprint(db) == before, "a run writes only simulation rows"
    assert len(base["llm"].users) == calls, "a rules-only run never calls the model"

    assert out["status"] == "completed" and out["started_at"] and out["finished_at"]
    rows = by_requirement(base, sid)
    lead = rows[base["lead"]]
    assert (lead["baseline_status"], lead["control_status"], lead["simulated_status"], lead["how"]) == (
        "not_met", "not_met", "needs_confirmation", "rule")
    assert [x["kind"] for x in lead["simulated"]["reached"]] == ["direct"] and lead["simulated"]["effect"]
    # Atlas's 45 days is declared, not proven: the rules say it now meets the bound but must be confirmed.
    assert lead["simulated"]["simulated"]["observed"] and lead["simulated"]["control"]["status"] == "not_met"
    s = out["summary"]
    assert [c["requirement_id"] for c in s["changed"]] == [base["lead"]]
    assert s["drift"] == []
    assert s["recommendation"]["control"].startswith("Blocker found")
    assert not s["recommendation"]["simulated"].startswith("Blocker found")
    assert rows[base["install"]]["how"] == "not_affected", "only the model reads it, but nothing the change touches reaches it"
    assert s["recommendation"]["complete"] is True, "so its baseline verdict still holds, and the recommendation is exact"


def test_control_reproduces_the_live_rule_verdicts_exactly(base):
    """With no change and no drift, the simulation's rules agree with the live run: same status, same reason."""
    db = base["db"]
    sid = simulate(base, [])
    engine.run(db, base["atlas"], sid)
    live = {r["requirement_id"]: r for r in db.execute(
        "SELECT requirement_id, status, reason, decided_by FROM verdict WHERE assessment_id = %s", (base["a"],))}
    db.rollback()
    rows = by_requirement(base, sid)
    rule_decided = [rid for rid, v in live.items() if v["decided_by"] == "rule"]
    assert len(rule_decided) == 3
    for rid in rule_decided:
        assert rows[rid]["control_status"] == rows[rid]["simulated_status"] == live[rid]["status"]
        assert rows[rid]["simulated"]["control"]["reason"] == live[rid]["reason"]
    assert status(base, sid)["summary"]["changed"] == []


def test_a_requirement_only_the_model_can_decide_is_never_guessed(base):
    """The installation requirement has no rule. A deadline change reaches it (the model is told the deadline), so it
    would need the model to re-decide: it is reported as such, with no status, and the recommendation is incomplete."""
    sid = simulate(base, [{"kind": "set_notice_field", "field": "notice.deadline", "value": "2099-06-30"}])
    engine.run(base["db"], base["atlas"], sid)
    install = by_requirement(base, sid)[base["install"]]
    assert install["how"] == "needs_model"
    assert install["control_status"] is None and install["simulated_status"] is None
    assert install["baseline_status"] == "met", "the live verdict is reported as it was, not recomputed"
    s = status(base, sid)["summary"]
    assert s["needs_model"] == [base["install"]] and s["recommendation"]["complete"] is False


def test_new_wording_is_reported_as_needing_the_model(base):
    sid = simulate(base, [{"kind": "set_requirement_text", "requirement_id": base["lead"], "statement": "Deliver within 60 days"}])
    engine.run(base["db"], base["atlas"], sid)
    lead = by_requirement(base, sid)[base["lead"]]
    assert lead["how"] == "needs_model" and lead["simulated_status"] is None
    assert lead["control_status"] == "not_met", "the control still has the original wording"
    assert "only the model" in lead["simulated"]["notes"][0]


def test_a_fact_change_reaches_the_requirements_that_used_the_fact(base):
    capacity = base["facts"]["capacity"]
    sid = simulate(base, [{"kind": "set_fact_value", "fact_id": capacity, "key": "lead_days", "value": 25}])
    engine.run(base["db"], base["atlas"], sid)
    rows = by_requirement(base, sid)
    assert (rows[base["lead"]]["control_status"], rows[base["lead"]]["simulated_status"]) == ("not_met", "needs_confirmation")
    lead = rows[base["lead"]]["simulated"]["reached"]
    assert {(x["kind"], x["target"]) for x in lead} == {("used", f"fact:{capacity}")}, "the baseline verdict used this fact"
    assert rows[base["standard"]]["how"] == "not_affected", "a certificate requirement never reads capacity facts"


def test_runs_are_deterministic(base):
    changes = [{**LEAD_OK, "requirement_id": base["lead"]}, {"kind": "set_notice_field", "field": "lot.quantity", "value": 400}]
    a, b = simulate(base, changes), simulate(base, changes)
    sa, sb = engine.run(base["db"], base["atlas"], a), engine.run(base["db"], base["atlas"], b)
    strip = lambda rows: {k: {f: v for f, v in r.items() if f != "simulation_id"} for k, r in rows.items()}  # noqa: E731
    assert strip(by_requirement(base, a)) == strip(by_requirement(base, b))
    assert sa["summary"] == sb["summary"]


# --- Comparing like with like ----------------------------------------------------------------------------------------------

def test_a_live_edit_after_the_baseline_is_drift_not_an_effect(base):
    """Atlas shortens its lead time in the live profile after the baseline ran. A simulation with no changes must not
    credit that to a hypothesis: control already differs from baseline, and that is reported as drift."""
    db = base["db"]
    capacity = base["facts"]["capacity"]
    db.execute("""UPDATE profile_fact SET value = jsonb_set(value, '{lead_days}', '20'), updated_at = now() WHERE id = %s""",
               (capacity,))
    db.commit()
    sid = simulate(base, [])
    assert status(base, sid)["drift"]["facts_changed_since_baseline"] == [capacity]
    engine.run(db, base["atlas"], sid)
    s = status(base, sid)["summary"]
    assert s["changed"] == [], "no change was made, so nothing is an effect"
    assert [d["requirement_id"] for d in s["drift"]] == [base["lead"]]
    assert s["drift"][0]["baseline"] == "not_met" and s["drift"][0]["control"] != "not_met"


# --- The decide() refactor -------------------------------------------------------------------------------------------------

def _lead_inputs(b):
    db = b["db"]
    req = db.execute("SELECT * FROM requirement WHERE id = %s", (b["lead"],)).fetchone()
    cond = db.execute("SELECT * FROM requirement_condition WHERE requirement_id = %s", (b["lead"],)).fetchall()
    _, ctx = lot_context(db, b["lot"], TODAY, b["vid"])
    facts = load_facts(db, b["atlas"], b["lot"], b["vid"])
    open_part = {"id": None, "ordinal": 9, "label": "installed by trained staff", "condition": None, "quote": "", "mandatory": True}
    return db, req, [dict(c) for c in cond] or [{"id": None, "ordinal": 0, "label": req["statement"], "condition": req["condition"],
                                                  "quote": req["quote"], "mandatory": True}], open_part, ctx, facts


def test_without_interpretation_a_failed_rule_still_decides_the_requirement(base):
    """A mandatory part a rule fails makes the whole requirement not met, whatever the model would say about the rest."""
    db, req, conds, open_part, ctx, facts = _lead_inputs(base)
    d = decide(db, base["vid"], req, facts, ctx, engine.NO_MODEL, 0.0, profile_id=base["atlas"],
               conditions=conds + [open_part], interpret=False)
    assert (d.status, d.decided_by) == (NOT_MET, "rule") and "not interpreted" in d.reason

    relaxed = [{**conds[0], "condition": {**conds[0]["condition"], "bound": 45}}]
    d = decide(db, base["vid"], req, facts, ctx, engine.NO_MODEL, 0.0, profile_id=base["atlas"],
               conditions=relaxed + [open_part], interpret=False)
    assert d.decided_by == "needs_model", "nothing fails it, and the rest needs reading: the model is not replaced by a guess"
    db.rollback()


def test_given_conditions_replace_the_stored_ones_only_when_passed(base):
    db, req, conds, _, ctx, facts = _lead_inputs(base)
    stored = decide(db, base["vid"], req, facts, ctx, engine.NO_MODEL, 0.0, profile_id=base["atlas"], interpret=False)
    same = decide(db, base["vid"], req, facts, ctx, engine.NO_MODEL, 0.0, profile_id=base["atlas"], conditions=conds,
                  interpret=False)
    assert (stored.status, stored.reason) == (same.status, same.reason) == (NOT_MET, stored.reason)
    db.rollback()


def test_the_stand_in_model_refuses_every_call():
    with pytest.raises(RuntimeError, match="must not call the model"):
        engine.NO_MODEL.json("verdict", "", "", {})


# --- Lifecycle -------------------------------------------------------------------------------------------------------------

def test_a_simulation_runs_once_and_its_hypothesis_is_then_frozen(base):
    db = base["db"]
    sid = simulate(base, [{**LEAD_OK, "requirement_id": base["lead"]}])
    engine.run(db, base["atlas"], sid)
    with pytest.raises(SimulationError, match="runs once; this one is completed"):
        engine.run(db, base["atlas"], sid)
    with pytest.raises(SimulationError, match="before a simulation runs"):
        store.add_change(db, base["atlas"], sid, {"kind": "set_notice_field", "field": "lot.quantity", "value": 1})
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE simulation_result SET how = 'rule' WHERE simulation_id = %s", (sid,))
    db.rollback()
    assert db.execute("SELECT status FROM simulation WHERE id = %s", (sid,)).fetchone()["status"] == "completed"
    db.rollback()


def test_another_supplier_cannot_run_a_simulation(base):
    sid = simulate(base, [])
    with pytest.raises(SimulationNotFound):
        engine.run(base["db"], base["pennine"], sid)
    assert status(base, sid)["status"] == "created"


def test_discarding_a_completed_simulation_keeps_its_results_and_live_state(base):
    db = base["db"]
    sid = simulate(base, [{**LEAD_OK, "requirement_id": base["lead"]}])
    engine.run(db, base["atlas"], sid)
    before = live_fingerprint(db)
    store.discard(db, base["atlas"], sid)
    assert live_fingerprint(db) == before
    assert status(base, sid)["status"] == "discarded" and len(by_requirement(base, sid)) == 4


# --- 8. A failure leaves no partial state ----------------------------------------------------------------------------------

def test_a_failure_while_evaluating_marks_the_run_failed_and_writes_no_results(base, monkeypatch):
    db = base["db"]
    sid = simulate(base, [{**LEAD_OK, "requirement_id": base["lead"]}])
    before = live_fingerprint(db)
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("rule engine exploded")
        return real(*args, **kwargs)

    real = engine.decide
    monkeypatch.setattr(engine, "decide", broken)
    with pytest.raises(RuntimeError, match="exploded"):
        engine.run(db, base["atlas"], sid)
    assert live_fingerprint(db) == before
    row = status(base, sid)
    assert (row["status"], row["error_stage"], row["summary"]) == ("failed", "evaluation", None)
    assert "exploded" in row["error"] and row["finished_at"]
    assert by_requirement(base, sid) == {}
    store.discard(db, base["atlas"], sid)  # a failed simulation can be cleared away
    assert status(base, sid)["status"] == "discarded"


def test_a_failure_while_recording_leaves_no_result_rows(base, monkeypatch):
    db = base["db"]
    sid = simulate(base, [])
    before = live_fingerprint(db)

    def half_written(conn, simulation_id, rows, summary):
        r = rows[0]
        conn.execute("""INSERT INTO simulation_result (simulation_id, requirement_id, how, simulated)
                        VALUES (%s, %s, %s, %s)""", (simulation_id, r.requirement_id, r.how, Jsonb({})))
        raise RuntimeError("disk full")

    monkeypatch.setattr(engine, "_record", half_written)
    with pytest.raises(RuntimeError, match="disk full"):
        engine.run(db, base["atlas"], sid)
    assert live_fingerprint(db) == before
    assert by_requirement(base, sid) == {}, "the first result was rolled back with the rest"
    row = status(base, sid)
    assert (row["status"], row["error_stage"]) == ("failed", "recording")


def test_a_write_attempted_during_evaluation_is_refused_by_postgres(base, monkeypatch):
    """Reused assessment code that tried to write (as a model call would, to llm_call and verdict_cache) cannot."""
    db = base["db"]
    sid = simulate(base, [])
    before = live_fingerprint(db)

    def writes(conn, *args, **kwargs):
        conn.execute("INSERT INTO verdict_cache (key, result) VALUES ('poison', '{}')")
        return {}

    monkeypatch.setattr(engine, "decide", writes)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        engine.run(db, base["atlas"], sid)
    assert live_fingerprint(db) == before
    assert db.execute("SELECT count(*) n FROM verdict_cache WHERE key = 'poison'").fetchone()["n"] == 0
    db.rollback()
    assert status(base, sid)["status"] == "failed"


def test_a_run_never_commits_the_callers_open_work(base):
    db = base["db"]
    sid = simulate(base, [])
    db.execute("UPDATE profile_fact SET label = 'uncommitted' WHERE id = %s", (base["facts"]["capacity"],))
    with pytest.raises(RuntimeError, match="finish or roll back"):
        engine.run(db, base["atlas"], sid)
    db.rollback()
    assert db.execute("SELECT label FROM profile_fact WHERE id = %s", (base["facts"]["capacity"],)).fetchone()["label"] != "uncommitted"
    db.rollback()
    assert status(base, sid)["status"] == "created"


def test_a_run_interrupted_mid_way_is_closed_and_can_then_be_discarded(base):
    db = base["db"]
    sid = simulate(base, [])
    db.execute("UPDATE simulation SET status = 'running', started_at = now() - interval '2 hours' WHERE id = %s", (sid,))
    db.commit()
    with pytest.raises(SimulationError, match="cannot be discarded until it finishes"):
        store.discard(db, base["atlas"], sid)
    assert engine.recover_stale_simulations(db) == 1
    row = status(base, sid)
    assert (row["status"], row["error_stage"]) == ("failed", "interrupted")
    store.discard(db, base["atlas"], sid)


def test_every_change_kind_runs(base):
    """All nine kinds together: the run completes, and nothing live is touched."""
    db = base["db"]
    sid = simulate(base, valid_changes(base))
    before = live_fingerprint(db)
    out = engine.run(db, base["atlas"], sid)
    assert live_fingerprint(db) == before
    assert out["status"] == "completed" and len(by_requirement(base, sid)) == 4


# --- Living alongside the demo -----------------------------------------------------------------------------------------------

def test_the_demo_reset_still_works_when_a_demo_lot_has_simulations(demo):
    """Simulations point at the demo's lot, assessment, version, requirements and verdicts; the reset must remove them
    first rather than fail on those foreign keys."""
    db, bright, _, lots = demo
    aid = db.execute("""SELECT id FROM assessment WHERE profile_id = %s AND status = 'done' AND NOT shadow AND NOT superseded
                        ORDER BY id DESC LIMIT 1""", (bright,)).fetchone()["id"]
    db.rollback()
    sid = store.create(db, bright, aid, "tester", as_of=TODAY)
    engine.run(db, bright, sid)
    reset(db, ARGS)
    assert db.execute("SELECT count(*) n FROM opportunity").fetchone()["n"] == 0
    assert db.execute("SELECT count(*) n FROM simulation").fetchone()["n"] == 0
    db.rollback()
