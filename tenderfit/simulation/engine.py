"""Running a simulation: the live verdict logic, evaluated on a frozen snapshot with and without the hypothesis.

Three phases, each with its own transaction:
  1. start:     created -> running, a guarded update, committed (a second run of the same simulation loses the race);
  2. evaluate:  everything is read and decided inside a READ ONLY transaction, so even the reused assessment code cannot
                write to a live table: Postgres refuses it;
  3. record:    results and completed status in one write transaction, touching only this simulation's rows.
Any failure after the start marks the simulation failed, with the stage it failed in, and is raised again.

A run holds a session-level advisory lock on its own connection from before the start until after its final status is
committed. A process that dies mid-run loses its connection, and Postgres releases the lock; close_abandoned_runs closes a
running simulation only when it can take that lock, so it never closes a run that is still alive, however long it takes.

Only requirements a change can reach (impact.reach) are recomputed; every other one keeps its control verdict.
selective=False recomputes them all and raises if one of those it would have skipped comes out differently.

Evaluation is rules-only: decide(interpret=False) never calls the model, reads no verdict cache and spends nothing. A
requirement only the model can decide is reported as "needs_model", never guessed."""
from typing import cast

from psycopg.types.json import Jsonb

from tenderfit.assessment.config import BASELINES, PipelineConfig
from tenderfit.assessment.confirm import overrides
from tenderfit.assessment.pipeline import Decision, _lineage, _source_label, decide, lot_dimensions
from tenderfit.changes.impact import load_deps
from tenderfit.config import settings
from tenderfit.db import Conn, must
from tenderfit.documents import service as docs
from tenderfit.llm import LLM
from tenderfit.rules import aggregate as agg
from tenderfit.rules.engine import NEEDS, NOT_MET, Context
from tenderfit.simulation import attribution, inputs, provenance, store
from tenderfit.simulation.changes import SimulationConflict, SimulationError
from tenderfit.simulation.compare import Row, summarise
from tenderfit.simulation.impact import reach
from tenderfit.simulation.isolation import read_only, require_idle
from tenderfit.simulation.scenario import Scenario, apply_changes
from tenderfit.simulation.snapshot import Snapshot

STALE_RUNNING_MINUTES = 30
RUN_LOCK = 0x53494D  # namespace of the two-key advisory locks held by running simulations ("SIM")
INTERRUPTED = "interrupted: the run stopped before it finished"
REQUIREMENT_FIELDS = ("statement", "quote", "category", "obligation", "evidence_needed", "equivalent_allowed", "passage_id")


class _NoModel:
    """Handed to decide() in place of a model. interpret=False means it is never reached; if it ever were, the run fails."""

    def json(self, *args, **kwargs):
        raise RuntimeError("a simulation must not call the model")

    def tool_turn(self, *args, **kwargs):
        raise RuntimeError("a simulation must not call the model")


NO_MODEL = cast(LLM, _NoModel())


def default_config() -> PipelineConfig:
    """The configuration a live assessment uses when none is given (assess_lot and _run). The agent setting only matters
    once the model is involved, which a rules-only simulation never is."""
    if settings.versionrag_enabled:
        return BASELINES["VersionRAG"]
    return PipelineConfig("B3" if settings.agent_enabled else "B2", agent=settings.agent_enabled)


def _lock_key(simulation_id: int) -> tuple[int, int]:
    return RUN_LOCK, simulation_id % 2147483647  # the two-key form takes int4s; a collision only makes recovery wait


def run(conn: Conn, profile_id: int, simulation_id: int, config: PipelineConfig | None = None, selective: bool = True) -> dict:
    require_idle(conn)
    got = conn.execute("SELECT pg_try_advisory_lock(%s, %s) AS got", _lock_key(simulation_id)).fetchone()
    conn.rollback()  # the lock is the session's, not the transaction's: it outlives this rollback
    if not (got and got["got"]):
        status = store.get(conn, profile_id, simulation_id)["status"]  # SimulationNotFound for another supplier
        conn.rollback()
        raise SimulationConflict(f"a simulation runs once; this one is {status}")
    try:
        return _run_locked(conn, profile_id, simulation_id, config, selective)
    finally:
        _unlock(conn, simulation_id)


def _unlock(conn: Conn, simulation_id: int) -> None:
    """Released after the final status is committed. If the connection is gone, so is the lock."""
    try:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s, %s)", _lock_key(simulation_id))
        conn.rollback()
    except Exception:
        pass


def _run_locked(conn: Conn, profile_id: int, simulation_id: int, config: PipelineConfig | None, selective: bool) -> dict:
    try:
        started = conn.execute(
            """UPDATE simulation SET status = 'running', started_at = now()
               WHERE id = %s AND profile_id = %s AND status = 'created' RETURNING id""", (simulation_id, profile_id)).fetchone()
        if not started:
            status = store.get(conn, profile_id, simulation_id)["status"]  # SimulationNotFound for another supplier
            raise SimulationConflict(f"a simulation runs once; this one is {status}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    stage = "evaluation"
    try:
        with read_only(conn):
            rows, summary = _evaluate(conn, profile_id, simulation_id, config or default_config(), selective)
        stage = "recording"
        _record(conn, simulation_id, rows, summary)
    except BaseException as e:
        conn.rollback()
        _mark_failed(conn, simulation_id, stage, e)
        raise
    try:
        return store.get(conn, profile_id, simulation_id)
    finally:
        conn.rollback()  # hand the connection back idle, as it was given


def _mark_failed(conn: Conn, simulation_id: int, stage: str, error: BaseException) -> None:
    try:
        conn.execute("""UPDATE simulation SET status = 'failed', finished_at = now(), error = %s, error_stage = %s
                        WHERE id = %s AND status = 'running'""",
                     (f"{type(error).__name__}: {error}"[:2000], stage, simulation_id))
        conn.commit()
    except Exception:  # the connection itself may be gone; recover_stale_simulations closes the run later
        conn.rollback()


def close_abandoned_runs(conn: Conn, profile_id: int | None = None, simulation_id: int | None = None,
                         older_than_minutes: int | None = None) -> int:
    """Close running simulations whose run is no longer alive, so they can be discarded. Returns how many were closed.

    A process that died mid-run cannot mark its simulation failed. Its run lock went with its connection, so a running
    simulation whose lock can be taken has no live run. One whose lock is held is left alone, however old it is. The
    transaction-level lock taken here also makes concurrent recoveries close each run once."""
    require_idle(conn)
    try:
        ids = [r["id"] for r in conn.execute(
            """SELECT id FROM simulation WHERE status = 'running' AND (%s::bigint IS NULL OR profile_id = %s)
                 AND (%s::bigint IS NULL OR id = %s)
                 AND (%s::int IS NULL OR started_at < now() - make_interval(mins => %s)) ORDER BY id""",
            (profile_id, profile_id, simulation_id, simulation_id, older_than_minutes, older_than_minutes))]
        closed = 0
        for sid in ids:
            if not must(conn.execute("SELECT pg_try_advisory_xact_lock(%s, %s) AS got", _lock_key(sid)).fetchone())["got"]:
                continue  # a live run holds it
            closed += conn.execute(
                """UPDATE simulation SET status = 'failed', finished_at = now(), error_stage = 'interrupted', error = %s
                   WHERE id = %s AND status = 'running'""", (INTERRUPTED, sid)).rowcount
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return closed


def recover_stale_simulations(conn: Conn, minutes: int = STALE_RUNNING_MINUTES) -> int:
    """All suppliers' abandoned runs older than `minutes`, for a periodic sweep. A run that is still alive is never closed."""
    return close_abandoned_runs(conn, older_than_minutes=minutes)


def impact(conn: Conn, sim: dict, snap: Snapshot, simulated: Scenario) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """(what the hypothesis reaches, what live edits since the baseline reach), per requirement. Reads only."""
    deps = load_deps(conn, snap.assessment_id)
    reqs = [{**r, "conditions": r["conditions"] + simulated.requirements[r["id"]]["conditions"]} for r in snap.requirements]
    facts = snap.facts + [f for f in simulated.facts if f.id < 0]
    hyp = reach(reqs, simulated.touched, deps, facts)
    drifted = {("fact", str(f)) for f in sim["drift"].get("facts_changed_since_baseline", [])}
    stale = reach(reqs, drifted, deps, facts) if drifted else {}
    return ({rid: [x.json() for x in rs] for rid, rs in hyp.items()},
            {rid: [x.json() for x in rs] for rid, rs in stale.items()})


def preview(conn: Conn, profile_id: int, simulation_id: int) -> dict[int, list[dict]]:
    """Before running: which requirements the recorded hypothesis can reach, and why. Writes nothing."""
    with read_only(conn):
        sim = store.get(conn, profile_id, simulation_id)
        snap = Snapshot.from_json(sim["snapshot"])
        return impact(conn, sim, snap, apply_changes(snap, sim["changes"]))[0]


def _evaluate(conn: Conn, profile_id: int, simulation_id: int, config: PipelineConfig,
              selective: bool = True) -> tuple[list[Row], dict]:
    sim = store.get(conn, profile_id, simulation_id)
    snap = Snapshot.from_json(sim["snapshot"])
    if snap.sha256() != sim["snapshot_sha256"]:
        raise SimulationError("the stored snapshot does not match its hash")
    control = apply_changes(snap, [])
    simulated = apply_changes(snap, sim["changes"])
    live = {r["id"]: r for r in conn.execute("SELECT * FROM requirement WHERE id = ANY(%s)",
                                             ([r["id"] for r in snap.requirements],))}
    overridden = overrides(conn, profile_id) if snap.overridden is None else {}
    reached, stale = impact(conn, sim, snap, simulated)
    baseline = must(conn.execute("SELECT recommendation, finished_at, created_at FROM assessment WHERE id = %s",
                                 (snap.assessment_id,)).fetchone())
    # Read in this same REPEATABLE READ transaction, so the provenance is exactly what control and simulated used.
    at_creation = sim["drift"].get("live_inputs_at_creation")
    at_run = inputs.read(conn, snap)
    since_creation = inputs.compare(at_creation, at_run)
    since_baseline = inputs.since_baseline(conn, snap, baseline["finished_at"] or baseline["created_at"])
    rows, vs_control, vs_simulated = [], [], []
    for req in snap.requirements:
        row = live.get(req["id"])
        if row is None:
            raise SimulationError(f"requirement {req['id']} of the baseline no longer exists")
        c = _decide(conn, snap, control, row, config)
        why = reached[req["id"]]
        s = _decide(conn, snap, simulated, row, config) if why or not selective else c
        if not why and s != c:
            raise SimulationError(f"requirement {req['id']} changed although no change reaches it: the dependency analysis is incomplete")
        # Attach context after equivalence checking: provenance differs even when the decision is identical.
        if c is not None:
            c = {**c, "provenance": provenance.record(c, control, simulated, req["id"], control=True)}
        if s is not None:
            s = {**s, "provenance": provenance.record(s, simulated, simulated, req["id"])}
        r = Row(req["id"], req["statement"], req["obligation"], req["baseline_verdict_id"], req["baseline_status"], c, s, why)
        if simulated.requirements[req["id"]].get("text_changed"):
            r.notes.append("the wording changed: only the model can read new wording")
        if c is None and stale.get(req["id"]):
            r.notes.append("baseline may be out of date: " + "; ".join(x["detail"] for x in stale[req["id"]]))
        for when, entries in (("since this simulation was created", since_creation), ("since the baseline ran", since_baseline)):
            for e in entries:
                if e["requirement_id"] in (req["id"], None) and e["input"] != "all":
                    r.input_drift.append({**e, "since": "creation" if when.endswith("created") else "baseline"})
                    if e["requirement_id"] == req["id"]:
                        usage = "the value was frozen at creation" if snap.answers is not None and e["input"] in ("answers", "overrides") else "control and simulated both use the current value"
                        r.notes.append(f"{when}, {e['detail']}; {usage}")
        rows.append(r)
        is_overridden = any(i in overridden for i in _lineage(conn, row)) if snap.overridden is None else False
        label = _source_label(conn, row)
        vs_control.append(_v(req, c, control.overridden.get(req["id"], False) if control.overridden is not None else is_overridden, label))
        vs_simulated.append(_v(req, s, simulated.overridden.get(req["id"], False) if simulated.overridden is not None else is_overridden, label))
    attributed = attribution.analyze(rows, snap, sim["changes"],
                                     lambda state, rid: _decide(conn, snap, state, live[rid], config))
    inv = docs.inventory(conn, snap.version_id, profile_id)
    rec_control = lot_dimensions(conn, snap.version_id, profile_id, vs_control, inv, control.facts, _ctx(control), config)
    rec_simulated = lot_dimensions(conn, snap.version_id, profile_id, vs_simulated, inv, simulated.facts, _ctx(simulated), config)
    execution = {
        "snapshot_sha256": sim["snapshot_sha256"],  # covers the frozen part only
        "frozen": list(inputs.FROZEN) + (["evidence references and verification at creation"] if snap.support is not None else []) + (["answers", "overrides"] if snap.answers is not None else []),
        "live_inputs": {name: {"tables": list(inputs.TABLES[name]), "at_run": d["sha256"],
                               "at_creation": ((at_creation or {}).get(name) or {}).get("sha256"),
                               "changed": ((at_creation or {}).get(name) or {}).get("sha256") != d["sha256"]}
                        for name, d in at_run.items()},
        "transaction": "control and simulated were read in one REPEATABLE READ, READ ONLY transaction",
    }
    summary = summarise(rows, baseline["recommendation"], rec_control.recommendation, rec_simulated.recommendation,
                           config.name, input_drift={"since_creation": since_creation, "since_baseline": since_baseline},
                           execution=execution)
    summary["attribution"] = attributed
    summary["override_changes"] = [{"requirement_id": rid, "before": (control.overridden or {}).get(rid, False), "after": value,
                                    "detail": "Hypothetical override changes aggregation only; the requirement verdict is unchanged."}
                                   for rid, value in (simulated.overridden or {}).items()
                                   if value != (control.overridden or {}).get(rid, False)]
    return rows, summary


def _ctx(s: Scenario) -> Context:
    return Context(s.as_of, s.fields)


def _decide(conn: Conn, snap: Snapshot, s: Scenario, live: dict, config: PipelineConfig) -> dict | None:
    """The live decide(), on the scenario's facts, context and conditions. None: only the model could decide it."""
    req = s.requirements[live["id"]]
    if req.get("text_changed"):
        return None
    full = {**live, **{k: req[k] for k in REQUIREMENT_FIELDS}}
    d = decide(conn, snap.version_id, full, s.facts, _ctx(s), NO_MODEL, 0.0, config=config, profile_id=snap.profile_id,
               conditions=req["conditions"], interpret=False,
               answers=s.answers.get(req["id"], []) if s.answers is not None else None)
    if d.decided_by == "needs_model":
        return None
    if d.decided_by != "rule" or d.cost or d.tokens:
        raise SimulationError(f"requirement {live['id']} was decided by {d.decided_by}, not by rules")
    return _record_of(d)


def _record_of(d: Decision) -> dict:
    return {"status": d.status, "reason": d.reason, "basis": d.basis, "facts": d.facts, "params": d.params,
            "required": d.required, "observed": d.observed, "question": d.question, "needs_human": d.needs_human,
            "conditions": d.conditions, "gaps": d.gaps, "conflict": d.conflict,
            "citations": [{"passage_id": pid, "quote": quote} for pid, quote in d.citations]}


def _v(req: dict, rec: dict | None, overridden: bool, label: str) -> agg.V:
    """As the live run aggregates: a verdict only the model gave stays as it was, and a recorded override of a not met
    counts as needs confirmation."""
    status = rec["status"] if rec else req["baseline_status"]
    if status == NOT_MET and overridden:
        status = NEEDS
    return agg.V(req["id"], req["statement"], req["obligation"], req["category"], status,
                 rec["reason"] if rec else "", label, rec["question"] if rec else None, rec["basis"] if rec else None)


def _record(conn: Conn, simulation_id: int, rows: list[Row], summary: dict) -> None:
    for r in rows:
        conn.execute(
            """INSERT INTO simulation_result (simulation_id, requirement_id, baseline_verdict_id, baseline_status, control_status,
                                              simulated_status, how, simulated)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (simulation_id, r.requirement_id, r.baseline_verdict_id, r.baseline_status, r.control_status, r.simulated_status,
             r.how, Jsonb({"simulated": r.simulated, "control": r.control, "reached": r.reached, "effect": r.effect,
                           "drift": r.drift, "notes": r.notes, "input_drift": r.input_drift, "attribution": r.attribution})))
    done = conn.execute("""UPDATE simulation SET status = 'completed', finished_at = now(), summary = %s
                           WHERE id = %s AND status = 'running' RETURNING id""", (Jsonb(summary), simulation_id)).fetchone()
    if not done:
        raise SimulationError("this run was closed before it could finish")
    conn.commit()
