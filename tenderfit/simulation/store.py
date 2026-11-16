"""Simulation records: the only code that writes simulation data, and it writes nothing else.

Every function is scoped to one supplier: a simulation or baseline of another supplier is reported as not found. Each
mutating function owns its transaction (it requires an idle connection, commits on success and rolls back on any error),
so a failure never leaves a half-written simulation and never touches the caller's own work."""
from datetime import date

from psycopg.types.json import Jsonb

from tenderfit.db import Conn, must
from tenderfit.simulation.changes import HypotheticalChange, SimulationError, SimulationNotFound, dump_change, parse_change
from tenderfit.simulation.isolation import read_only, require_idle
from tenderfit.simulation.scenario import Scenario, apply_changes
from tenderfit.simulation.snapshot import ENGINE_VERSION, Snapshot, take_snapshot

DISCARDABLE = ("created", "completed", "failed")


def create(conn: Conn, profile_id: int, baseline_assessment_id: int, created_by: str, title: str | None = None,
           as_of: date | None = None) -> int:
    """A new simulation of one completed assessment. The baseline is read in a READ ONLY transaction."""
    title = (title or "").strip() or None
    if title and len(title) > 500:
        raise SimulationError("keep the title under 500 characters")
    if not created_by.strip():
        raise SimulationError("say who created the simulation")
    with read_only(conn):
        snap, drift = take_snapshot(conn, profile_id, baseline_assessment_id, as_of)
    try:
        sid = must(conn.execute(
            """INSERT INTO simulation (profile_id, lot_id, baseline_assessment_id, version_id, title, as_of, snapshot,
                                       snapshot_sha256, drift, engine_version, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (profile_id, snap.lot_id, snap.assessment_id, snap.version_id, title, snap.as_of, Jsonb(snap.to_json()),
             snap.sha256(), Jsonb(drift), ENGINE_VERSION, created_by.strip())).fetchone())["id"]
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return sid


def _row(conn: Conn, profile_id: int, simulation_id: int, lock: bool = False) -> dict:
    row = conn.execute(f"SELECT * FROM simulation WHERE id = %s AND profile_id = %s{' FOR UPDATE' if lock else ''}",
                       (simulation_id, profile_id)).fetchone()
    if not row:
        raise SimulationNotFound("simulation not found")
    return row


def _changes(conn: Conn, simulation_id: int) -> list[HypotheticalChange]:
    return [parse_change(r["payload"]) for r in conn.execute(
        "SELECT payload FROM simulation_change WHERE simulation_id = %s ORDER BY ordinal", (simulation_id,))]


def add_change(conn: Conn, profile_id: int, simulation_id: int, change: object) -> int:
    """Validate one hypothetical change against the snapshot, after the ones already recorded, and record it. Returns its ordinal."""
    require_idle(conn)
    try:
        row = _row(conn, profile_id, simulation_id, lock=True)  # serialises concurrent additions to one simulation
        if row["status"] != "created":
            raise SimulationError(f"changes can only be added before a simulation runs; this one is {row['status']}")
        parsed = parse_change(change)
        existing = _changes(conn, simulation_id)
        apply_changes(Snapshot.from_json(row["snapshot"]), existing + [parsed])
        ordinal = len(existing) + 1
        conn.execute("INSERT INTO simulation_change (simulation_id, ordinal, kind, payload) VALUES (%s, %s, %s, %s)",
                     (simulation_id, ordinal, parsed.kind, Jsonb(dump_change(parsed))))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ordinal


def discard(conn: Conn, profile_id: int, simulation_id: int) -> None:
    """A soft, final state change. Nothing is deleted, and nothing outside this simulation's own rows is touched."""
    require_idle(conn)
    try:
        done = conn.execute(
            """UPDATE simulation SET status = 'discarded', discarded_at = now()
               WHERE id = %s AND profile_id = %s AND status = ANY(%s) RETURNING id""",
            (simulation_id, profile_id, list(DISCARDABLE))).fetchone()
        if not done:
            status = _row(conn, profile_id, simulation_id)["status"]
            raise SimulationError("this simulation is already discarded" if status == "discarded" else
                                  f"a {status} simulation cannot be discarded until it finishes")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def get(conn: Conn, profile_id: int, simulation_id: int) -> dict:
    row = _row(conn, profile_id, simulation_id)
    return {**row, "changes": [dump_change(c) for c in _changes(conn, simulation_id)]}


def list_for_profile(conn: Conn, profile_id: int, include_discarded: bool = False) -> list[dict]:
    return conn.execute(
        """SELECT s.id, s.lot_id, s.baseline_assessment_id, s.version_id, s.status, s.title, s.as_of, s.created_by, s.created_at,
                  (SELECT count(*) FROM simulation_change c WHERE c.simulation_id = s.id) AS changes
           FROM simulation s WHERE s.profile_id = %s AND (%s OR s.status <> 'discarded') ORDER BY s.created_at DESC, s.id DESC""",
        (profile_id, include_discarded)).fetchall()


def scenario(conn: Conn, profile_id: int, simulation_id: int) -> Scenario:
    """The simulation's snapshot with its recorded changes applied, rebuilt from what is stored."""
    row = _row(conn, profile_id, simulation_id)
    return apply_changes(Snapshot.from_json(row["snapshot"]), _changes(conn, simulation_id))
