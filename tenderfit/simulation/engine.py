"""Running a simulation: the live verdict logic, evaluated on a frozen snapshot with and without the hypothesis.

Three phases, each with its own transaction:
  1. start:     created -> running, a guarded update, committed (a second run of the same simulation loses the race);
  2. evaluate:  everything is read and decided inside a READ ONLY transaction, so even the reused assessment code cannot
                write to a live table: Postgres refuses it;
  3. record:    results and completed status in one write transaction, touching only this simulation's rows.
Any failure after the start marks the simulation failed, with the stage it failed in, and is raised again.

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
from tenderfit.db import Conn
from tenderfit.documents import service as docs
from tenderfit.llm import LLM
from tenderfit.rules import aggregate as agg
from tenderfit.rules.engine import NEEDS, NOT_MET, Context
from tenderfit.simulation import store
from tenderfit.simulation.changes import SimulationConflict, SimulationError
from tenderfit.simulation.compare import Row, summarise
from tenderfit.simulation.impact import reach
from tenderfit.simulation.isolation import read_only, require_idle
from tenderfit.simulation.scenario import Scenario, apply_changes
from tenderfit.simulation.snapshot import Snapshot

STALE_RUNNING_MINUTES = 30
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


def run(conn: Conn, profile_id: int, simulation_id: int, config: PipelineConfig | None = None, selective: bool = True) -> dict:
    require_idle(conn)
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


def recover_stale_simulations(conn: Conn, minutes: int = STALE_RUNNING_MINUTES) -> int:
    """A process that died mid-run cannot mark its simulation failed, and a running simulation cannot be discarded."""
    require_idle(conn)
    n = conn.execute(
        """UPDATE simulation SET status = 'failed', finished_at = now(), error_stage = 'interrupted',
                  error = 'interrupted: the run stopped before it finished'
           WHERE status = 'running' AND started_at < now() - make_interval(mins => %s)""", (minutes,)).rowcount
    conn.commit()
    return n


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
    overridden = overrides(conn, profile_id)
    reached, stale = impact(conn, sim, snap, simulated)
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
        r = Row(req["id"], req["statement"], req["obligation"], req["baseline_verdict_id"], req["baseline_status"], c, s, why)
        if simulated.requirements[req["id"]].get("text_changed"):
            r.notes.append("the wording changed: only the model can read new wording")
        if c is None and stale.get(req["id"]):
            r.notes.append("baseline may be out of date: " + "; ".join(x["detail"] for x in stale[req["id"]]))
        rows.append(r)
        is_overridden = any(i in overridden for i in _lineage(conn, row))
        label = _source_label(conn, row)
        vs_control.append(_v(req, c, is_overridden, label))
        vs_simulated.append(_v(req, s, is_overridden, label))
    inv = docs.inventory(conn, snap.version_id, profile_id)
    rec_control = lot_dimensions(conn, snap.version_id, profile_id, vs_control, inv, control.facts, _ctx(control), config)
    rec_simulated = lot_dimensions(conn, snap.version_id, profile_id, vs_simulated, inv, simulated.facts, _ctx(simulated), config)
    baseline = conn.execute("SELECT recommendation FROM assessment WHERE id = %s", (snap.assessment_id,)).fetchone()
    return rows, summarise(rows, baseline["recommendation"] if baseline else None, rec_control.recommendation,
                           rec_simulated.recommendation, config.name)


def _ctx(s: Scenario) -> Context:
    return Context(s.as_of, s.fields)


def _decide(conn: Conn, snap: Snapshot, s: Scenario, live: dict, config: PipelineConfig) -> dict | None:
    """The live decide(), on the scenario's facts, context and conditions. None: only the model could decide it."""
    req = s.requirements[live["id"]]
    if req.get("text_changed"):
        return None
    full = {**live, **{k: req[k] for k in REQUIREMENT_FIELDS}}
    d = decide(conn, snap.version_id, full, s.facts, _ctx(s), NO_MODEL, 0.0, config=config, profile_id=snap.profile_id,
               conditions=req["conditions"], interpret=False)
    if d.decided_by == "needs_model":
        return None
    if d.decided_by != "rule" or d.cost or d.tokens:
        raise SimulationError(f"requirement {live['id']} was decided by {d.decided_by}, not by rules")
    return _record_of(d)


def _record_of(d: Decision) -> dict:
    return {"status": d.status, "reason": d.reason, "basis": d.basis, "facts": d.facts, "params": d.params,
            "required": d.required, "observed": d.observed, "question": d.question, "needs_human": d.needs_human,
            "conditions": d.conditions, "gaps": d.gaps, "conflict": d.conflict}


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
                           "drift": r.drift, "notes": r.notes})))
    done = conn.execute("""UPDATE simulation SET status = 'completed', finished_at = now(), summary = %s
                           WHERE id = %s AND status = 'running' RETURNING id""", (Jsonb(summary), simulation_id)).fetchone()
    if not done:
        raise SimulationError("this run was closed before it could finish")
    conn.commit()
