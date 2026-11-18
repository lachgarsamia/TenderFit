"""Mode B over HTTP: the deterministic what-if simulations (rules only, synchronous) and the what-if agent's questions.

The only API module that imports tenderfit.simulation. It adds no logic of its own: every operation is one call to the
simulation package, which scopes it to the supplier, owns its transactions and enforces isolation (READ ONLY evaluation,
writes to simulation tables only). This module's job is to take the supplier from the session, never from the request,
and to map the package's errors to status codes:

  SimulationNotFound   404  unknown, or another supplier's: the caller cannot tell which
  SimulationConflict   409  valid, but not in the current state (already run, discarded, not a usable baseline)
  SimulationError      422  an invalid title or change; the message names the change and the field

Every response carrying simulation data says it is hypothetical. There is deliberately no route that applies a
simulation to the live assessment.

Before reading or acting on a simulation, its abandoned run (if any) is closed: a run whose process died stays `running`
otherwise, and could never be discarded. engine.close_abandoned_runs never closes a run that is still alive. It writes
only the simulation's own status, so even a GET touches nothing live.

A what-if question (POST /assessments/{id}/what-if) is answered in the background: the request creates the simulation,
records the question and queues one simulation_agent job (max_attempts=1), under the supplier's spend lock. It is refused
with 409 while the supplier already has a question queued or running, and with 429 when the day's allowance cannot cover
a whole session or the queue is full. While the agent works on a simulation, nobody else can change or run it (409)."""
import logging
import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from tenderfit.api.assessments import _lot
from tenderfit.api.deps import Identity, current_profile, current_user
from tenderfit.config import settings
from tenderfit.db import Conn, connect, must
from tenderfit.jobs import queue
from tenderfit.simulation import agent, engine, scenarios, spend, store
from tenderfit.simulation.changes import SimulationConflict, SimulationError, SimulationNotFound
from tenderfit.simulation.snapshot import Snapshot

log = logging.getLogger("tenderfit.api.simulations")
router = APIRouter(dependencies=[Depends(current_user)], tags=["simulations"])

NOTICE = ("Hypothetical: a what-if simulation of a frozen copy of your assessment. "
          "It does not change your assessment, your profile or your evidence.")
RUN_FAILED = "the simulation could not be completed and has been marked failed; nothing live was changed"


class SimulationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessment_id: int
    title: str | None = Field(default=None, max_length=500)
    as_of: date | None = None
    # The hypothesis, validated in full against the snapshot before anything is written (simulation.store.create).
    changes: list[dict] | None = Field(default=None, max_length=store.MAX_CHANGES)


class VariationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=500)
    changes: list[dict] | None = Field(default=None, max_length=store.MAX_CHANGES)


class WhatIfIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=agent.MAX_QUESTION)
    as_of: date | None = None


AGENT_KIND = "simulation_agent"


class ChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change: dict  # the same JSON as simulation.changes; validated by the simulation package, all nine kinds


@contextmanager
def _sim() -> Iterator[Conn]:
    """A connection whose simulation errors become HTTP errors. The simulation functions leave it idle."""
    with connect() as conn:
        try:
            yield conn
        except SimulationNotFound as e:
            raise HTTPException(404, str(e)) from None
        except SimulationConflict as e:
            raise HTTPException(409, str(e)) from None
        except SimulationError as e:
            raise HTTPException(422, str(e)) from None
        finally:
            conn.rollback()  # reads only: nothing outside the simulation package's own commits is ever kept


def _failure(row: dict) -> dict | None:
    if row["status"] != "failed":
        return None
    error = row["error"] or ""
    # A SimulationError is written for people; anything else may carry internals, which stay in the database and log.
    message = error.split(": ", 1)[1] if error.startswith(("SimulationError: ", "SimulationConflict: ")) else RUN_FAILED
    return {"stage": row["error_stage"], "message": message}


def _detail(conn: Conn, row: dict) -> dict:
    a = conn.execute("SELECT recommendation, superseded FROM assessment WHERE id = %s AND profile_id = %s",
                     (row["baseline_assessment_id"], row["profile_id"])).fetchone() or {}
    return {
        "hypothetical": True, "notice": NOTICE,
        "id": row["id"], "lot_id": row["lot_id"], "status": row["status"], "title": row["title"],
        "parent_id": row["parent_id"],
        "baseline": {"assessment_id": row["baseline_assessment_id"], "version_id": row["version_id"],
                     "recommendation": a.get("recommendation"), "superseded": a.get("superseded")},
        "as_of": row["as_of"], "snapshot_sha256": row["snapshot_sha256"], "engine_version": row["engine_version"],
        # What changed since the baseline, as seen at creation. The digests of the live inputs recorded at creation stay
        # internal; summary.execution reports, after a run, whether each input changed between creation and run.
        "drift": {k: v for k, v in row["drift"].items() if k != "live_inputs_at_creation"}, "changes": row["changes"], "summary": row["summary"], "failure": _failure(row),
        "created_by": row["created_by"], "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "discarded_at": row["discarded_at"],
    }


def _agent_jobs(conn: Conn, profile_id: int, simulation_id: int | None = None) -> list[dict]:
    """The supplier's what-if jobs still queued or running (for one simulation, or any)."""
    match = {"profile_id": profile_id, **({"simulation_id": simulation_id} if simulation_id is not None else {})}
    return conn.execute("""SELECT id, status, (payload->>'simulation_id')::bigint AS simulation_id FROM job
                           WHERE kind = %s AND status IN ('queued', 'running') AND payload @> %s ORDER BY id""",
                        (AGENT_KIND, Jsonb(match))).fetchall()


def _not_while_agent_works(conn: Conn, profile_id: int, simulation_id: int) -> None:
    busy = _agent_jobs(conn, profile_id, simulation_id)
    conn.rollback()
    if busy:
        raise SimulationConflict(f"the what-if agent is working on this simulation (job {busy[0]['id']}); "
                                 "wait for it to finish, or discard the simulation")


def _recover(conn: Conn, profile_id: int, simulation_id: int | None = None) -> None:
    """Close this supplier's abandoned runs (one simulation's, or all of them) before they are shown or acted on."""
    engine.close_abandoned_runs(conn, profile_id, simulation_id)


def _get(conn: Conn, profile_id: int, simulation_id: int) -> dict:
    out = _detail(conn, store.get(conn, profile_id, simulation_id))
    conn.rollback()
    return out


def _requirements(conn: Conn, profile_id: int, simulation_id: int) -> list[dict]:
    """The baseline's requirements as frozen in the snapshot, in order."""
    reqs = Snapshot.from_json(store.get(conn, profile_id, simulation_id)["snapshot"]).requirements
    conn.rollback()
    return reqs


@router.get("/lots/{lot_id}/simulations")
def list_simulations(lot_id: int, include_discarded: bool = False, limit: int = Query(50, ge=1, le=200),
                     profile_id: int = Depends(current_profile)) -> dict:
    with _sim() as conn:
        _lot(conn, lot_id)
        conn.rollback()
        _recover(conn, profile_id)
        rows = store.list_for_profile(conn, profile_id, include_discarded, lot_id=lot_id, limit=limit)
    return {"hypothetical": True, "notice": NOTICE, "simulations": rows}


@router.post("/simulations", status_code=201)
def create_simulation(body: SimulationIn, profile_id: int = Depends(current_profile),
                      me: Identity = Depends(current_user)) -> dict:
    with _sim() as conn:
        sid = store.create(conn, profile_id, body.assessment_id, me.email, title=body.title, as_of=body.as_of,
                           changes=body.changes)
        return _get(conn, profile_id, sid)


@router.post("/simulations/{simulation_id}/variations", status_code=201)
def create_variation(simulation_id: int, body: VariationIn, profile_id: int = Depends(current_profile),
                     me: Identity = Depends(current_user)) -> dict:
    """A new scenario on exactly the parent's frozen snapshot, with its own changes. The parent is not touched."""
    with _sim() as conn:
        _recover(conn, profile_id, simulation_id)
        sid = store.create_variation(conn, profile_id, simulation_id, me.email, title=body.title, changes=body.changes)
        return _get(conn, profile_id, sid)


@router.get("/simulations/{simulation_id}/inputs")
def simulation_inputs(simulation_id: int, profile_id: int = Depends(current_profile)) -> dict:
    """The scenario's frozen inputs (actual values only) and its hypothesis: what a variation starts from."""
    with _sim() as conn:
        return {"hypothetical": False, **scenarios.inputs_for_simulation(conn, profile_id, simulation_id)}


@router.get("/lots/{lot_id}/simulation-inputs")
def assessment_inputs(lot_id: int, assessment_id: int, profile_id: int = Depends(current_profile)) -> dict:
    """What a new scenario of this assessment could change: the actual requirements, conditions, facts and tender fields
    it would freeze. Creates nothing."""
    with _sim() as conn:
        _lot(conn, lot_id)
        conn.rollback()
        return {"hypothetical": False, **scenarios.inputs_for_assessment(conn, profile_id, assessment_id, lot_id)}


@router.get("/lots/{lot_id}/simulations/compare")
def compare(lot_id: int, ids: str = Query(..., max_length=200, description="2 to 4 simulation ids, comma separated"),
            profile_id: int = Depends(current_profile)) -> dict:
    """Completed scenarios of one assessment side by side: transitions by name, never a score."""
    try:
        wanted = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(422, "ids must be simulation ids separated by commas") from None
    if any(not 0 < x < 2**63 for x in wanted):
        raise HTTPException(422, "ids must be simulation ids separated by commas")
    with _sim() as conn:
        _lot(conn, lot_id)
        conn.rollback()
        _recover(conn, profile_id)
        return {"notice": NOTICE, **scenarios.compare(conn, profile_id, lot_id, wanted)}


@router.get("/simulations/{simulation_id}")
def get_simulation(simulation_id: int, profile_id: int = Depends(current_profile)) -> dict:
    with _sim() as conn:
        _recover(conn, profile_id, simulation_id)
        return _get(conn, profile_id, simulation_id)


@router.post("/simulations/{simulation_id}/changes", status_code=201)
def add_change(simulation_id: int, body: ChangeIn, profile_id: int = Depends(current_profile)) -> dict:
    with _sim() as conn:
        _not_while_agent_works(conn, profile_id, simulation_id)
        ordinal = store.add_change(conn, profile_id, simulation_id, body.change)
        changes = store.get(conn, profile_id, simulation_id)["changes"][:ordinal]
    out = {"hypothetical": True, "simulation_id": simulation_id, "ordinal": ordinal, "change": changes[-1]}
    if changes[-1]["kind"] == "add_fact":
        # Added facts are numbered -1, -2, ... in the order they were added (scenario.apply_changes), so a later change
        # can refer to this one.
        out["fact_id"] = -sum(c["kind"] == "add_fact" for c in changes)
    return out


@router.get("/simulations/{simulation_id}/preview")
def preview(simulation_id: int, profile_id: int = Depends(current_profile)) -> dict:
    with _sim() as conn:
        reached = engine.preview(conn, profile_id, simulation_id)
        reqs = _requirements(conn, profile_id, simulation_id)
    return {"hypothetical": True, "notice": NOTICE, "simulation_id": simulation_id,
            "reached": [{"requirement_id": r["id"], "statement": r["statement"], "why": reached[r["id"]]}
                        for r in reqs if reached.get(r["id"])],
            "not_reached": [{"requirement_id": r["id"], "statement": r["statement"]} for r in reqs if not reached.get(r["id"])]}


@router.post("/simulations/{simulation_id}/run")
def run(simulation_id: int, profile_id: int = Depends(current_profile)) -> dict:
    """Synchronous and rules-only. Exactly one of two simultaneous runs starts; the other gets 409."""
    with _sim() as conn:
        _recover(conn, profile_id, simulation_id)
        _not_while_agent_works(conn, profile_id, simulation_id)
        try:
            engine.run(conn, profile_id, simulation_id)
        except (SimulationConflict, SimulationNotFound):
            raise  # it never started: nothing was marked
        except Exception:
            # The run had started: the engine has rolled back its results and marked it failed, with the stage.
            log.exception("simulation %s failed", simulation_id)
            conn.rollback()
            raise HTTPException(500, RUN_FAILED) from None
        return _get(conn, profile_id, simulation_id)


@router.get("/simulations/{simulation_id}/results")
def results(simulation_id: int, profile_id: int = Depends(current_profile)) -> dict:
    with _sim() as conn:
        _recover(conn, profile_id, simulation_id)
        row = store.get(conn, profile_id, simulation_id)
        if row["status"] != "completed":
            raise SimulationConflict(f"results exist only for a completed simulation; this one is {row['status']}")
        reqs = {r["id"]: r for r in Snapshot.from_json(row["snapshot"]).requirements}
        stored = store.results(conn, profile_id, simulation_id)
    out = []
    for r in stored:
        sim, req = r["simulated"], reqs[r["requirement_id"]]
        out.append({
            "requirement_id": r["requirement_id"], "statement": req["statement"], "obligation": req["obligation"],
            "baseline_status": r["baseline_status"], "control_status": r["control_status"],
            "simulated_status": r["simulated_status"], "how": r["how"], "effect": sim.get("effect", False),
            "drift": sim.get("drift", False), "reached": sim.get("reached", []),
            "reason_control": (sim.get("control") or {}).get("reason"),
            "reason_simulated": (sim.get("simulated") or {}).get("reason"), "notes": sim.get("notes", []),
            "input_drift": sim.get("input_drift", []),
        })
    return {"hypothetical": True, "notice": NOTICE, "simulation_id": simulation_id, "summary": row["summary"], "results": out}


@router.post("/simulations/{simulation_id}/discard")
def discard(simulation_id: int, profile_id: int = Depends(current_profile)) -> dict:
    with _sim() as conn:
        _recover(conn, profile_id, simulation_id)
        store.discard(conn, profile_id, simulation_id)
        return _get(conn, profile_id, simulation_id)



# --- The what-if agent ------------------------------------------------------------------------------------------------

def _retry_after(at: datetime) -> str:
    return str(max(1, math.ceil((at - datetime.now(timezone.utc)).total_seconds())))


@router.post("/assessments/{assessment_id}/what-if", status_code=202)
def what_if(assessment_id: int, body: WhatIfIn, profile_id: int = Depends(current_profile),
            me: Identity = Depends(current_user)) -> dict:
    """Queue a what-if question for the agent. 202 with the simulation and job to follow (GET /jobs/{job_id},
    GET /simulations/{id}/trace). The supplier is always the session's."""
    with _sim() as conn:
        question = agent.check_question(body.question)
        with spend.supplier_lock(conn, profile_id):
            if active := _agent_jobs(conn, profile_id):
                raise HTTPException(409, {"message": "you already have a what-if question queued or running; "
                                                     "wait for it to finish", "simulation_id": active[0]["simulation_id"],
                                          "job_id": active[0]["id"]})
            if at := spend.frees_at(conn, profile_id, settings.simulation_agent_session_usd):
                left = spend.allowance(conn, profile_id)
                raise HTTPException(429, {"message": "today's what-if allowance cannot cover another question yet",
                                          "remaining_usd": left["remaining_usd"], "next_question_at": at.isoformat()},
                                    headers={"Retry-After": _retry_after(at)})
            queued = must(conn.execute("SELECT count(*) AS n FROM job WHERE kind = %s AND status = 'queued'",
                                       (AGENT_KIND,)).fetchone())["n"]
            conn.rollback()
            if queued >= settings.simulation_agent_queue_max:
                raise HTTPException(429, {"message": "too many what-if questions are waiting; try again shortly"},
                                    headers={"Retry-After": "30"})
            sid = agent.begin(conn, profile_id, assessment_id, question, me.email, body.as_of)
            try:
                job_id = queue.enqueue(conn, AGENT_KIND, {"profile_id": profile_id, "simulation_id": sid}, max_attempts=1)
                conn.commit()
            except BaseException:
                conn.rollback()
                store.discard(conn, profile_id, sid)  # never a question waiting for an agent that was not queued
                raise
    return {"hypothetical": True, "notice": NOTICE, "simulation_id": sid, "job_id": job_id}


@router.get("/what-if/allowance")
def what_if_allowance(profile_id: int = Depends(current_profile)) -> dict:
    with _sim() as conn:
        out = spend.allowance(conn, profile_id)
        busy = _agent_jobs(conn, profile_id)
    return {**out, "can_ask": out["can_ask"] and not busy,
            "active": {"simulation_id": busy[0]["simulation_id"], "job_id": busy[0]["id"]} if busy else None}


@router.get("/simulations/{simulation_id}/trace")
def trace(simulation_id: int, profile_id: int = Depends(current_profile)) -> dict:
    """The agent's steps for this simulation, and its accepted answer once it gave one. Empty for a simulation built by
    hand. cost_estimated marks a failed call whose cost is an estimate."""
    with _sim() as conn:
        steps = agent.trace(conn, profile_id, simulation_id)
        outcome = agent.answer(conn, profile_id, simulation_id)
        job = conn.execute("""SELECT id, status FROM job WHERE kind = %s AND payload @> %s ORDER BY id DESC LIMIT 1""",
                           (AGENT_KIND, Jsonb({"profile_id": profile_id, "simulation_id": simulation_id}))).fetchone()
    return {"hypothetical": True, "notice": NOTICE, "simulation_id": simulation_id,
            "job": {"id": job["id"], "status": job["status"]} if job else None, "outcome": outcome,
            "steps": [{"step": s["step"], "tool": s["tool"], "input": s["input"], "output": s["output"], "model": s["model"],
                       "tokens": {"input": s["input_tokens"], "output": s["output_tokens"]}, "cost_usd": float(s["cost_usd"]),
                       "cost_estimated": bool(s["output"].get("cost_estimated", False)), "created_at": s["created_at"]}
                      for s in steps]}
