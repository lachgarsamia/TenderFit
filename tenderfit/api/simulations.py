"""Mode B over HTTP: the deterministic what-if simulations, rules only, no model, synchronous.

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
only the simulation's own status, so even a GET touches nothing live."""
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from tenderfit.api.assessments import _lot
from tenderfit.api.deps import Identity, current_profile, current_user
from tenderfit.db import Conn, connect
from tenderfit.simulation import engine, store
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
        "baseline": {"assessment_id": row["baseline_assessment_id"], "version_id": row["version_id"],
                     "recommendation": a.get("recommendation"), "superseded": a.get("superseded")},
        "as_of": row["as_of"], "snapshot_sha256": row["snapshot_sha256"], "engine_version": row["engine_version"],
        # What changed since the baseline, as seen at creation. The digests of the live inputs recorded at creation stay
        # internal; summary.execution reports, after a run, whether each input changed between creation and run.
        "drift": {k: v for k, v in row["drift"].items() if k != "live_inputs_at_creation"}, "changes": row["changes"], "summary": row["summary"], "failure": _failure(row),
        "created_by": row["created_by"], "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "discarded_at": row["discarded_at"],
    }


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
        sid = store.create(conn, profile_id, body.assessment_id, me.email, title=body.title, as_of=body.as_of)
        return _get(conn, profile_id, sid)


@router.get("/simulations/{simulation_id}")
def get_simulation(simulation_id: int, profile_id: int = Depends(current_profile)) -> dict:
    with _sim() as conn:
        _recover(conn, profile_id, simulation_id)
        return _get(conn, profile_id, simulation_id)


@router.post("/simulations/{simulation_id}/changes", status_code=201)
def add_change(simulation_id: int, body: ChangeIn, profile_id: int = Depends(current_profile)) -> dict:
    with _sim() as conn:
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

