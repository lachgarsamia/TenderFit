"""Simulation records: the only code that writes simulation data, and it writes nothing else.

Every function is scoped to one supplier: a simulation or baseline of another supplier is reported as not found. Each
mutating function owns its transaction (it requires an idle connection, commits on success and rolls back on any error),
so a failure never leaves a half-written simulation and never touches the caller's own work."""
import json
from datetime import date

from psycopg.types.json import Jsonb

from tenderfit.db import Conn, must
from tenderfit.simulation import inputs
from tenderfit.simulation.changes import (
    HypotheticalChange,
    SimulationConflict,
    SimulationError,
    SimulationNotFound,
    dump_change,
    parse_change,
)
from tenderfit.simulation.isolation import read_only, require_idle
from tenderfit.simulation.scenario import Scenario, apply_changes
from tenderfit.simulation.snapshot import ENGINE_VERSION, Snapshot, take_snapshot

DISCARDABLE = ("created", "completed", "failed")
MAX_CHANGES = 25          # per simulation: a hypothesis, not a bulk edit
MAX_CHANGE_BYTES = 4000   # one change as JSON; add_fact's free-form value is the only large field


def _checked(changes: object) -> list[HypotheticalChange]:
    """Parse a list of changes from untrusted input, bounded in number and size. Validation against a snapshot is
    apply_changes's job; this only refuses what is malformed or oversized."""
    if not isinstance(changes, list):
        raise SimulationError("changes must be a list")
    if len(changes) > MAX_CHANGES:
        raise SimulationError(f"a simulation holds at most {MAX_CHANGES} changes")
    return [_one(c, n) for n, c in enumerate(changes, start=1)]


def _one(change: object, n: int | None = None) -> HypotheticalChange:
    try:
        size = len(json.dumps(change, default=str))
    except (TypeError, ValueError):
        raise SimulationError("invalid change: not JSON") from None
    if size > MAX_CHANGE_BYTES:
        raise SimulationError(f"change {n} is too large" if n else "this change is too large")
    try:
        return parse_change(change)
    except SimulationError as e:
        raise SimulationError(f"change {n}: {e}" if n else str(e)) from None


def create(conn: Conn, profile_id: int, baseline_assessment_id: int, created_by: str, title: str | None = None,
           as_of: date | None = None, changes: list | None = None) -> int:
    """A new simulation of one completed assessment, with an optional hypothesis. The baseline is read in a READ ONLY
    transaction; the simulation and every change are written in one transaction, so an invalid change creates nothing."""
    title, created_by = _who(title, created_by)
    parsed = _checked(changes or [])
    with read_only(conn):
        snap, drift = take_snapshot(conn, profile_id, baseline_assessment_id, as_of)
        # The live inputs the snapshot does not freeze, as they are now. drift cannot be updated later (019's guard).
        drift = {**drift, "live_inputs_at_creation": inputs.read(conn, snap)}
    apply_changes(snap, parsed)  # every change valid against the snapshot, in order, or SimulationError naming it
    return _insert(conn, profile_id, snap, snap.sha256(), drift, title, created_by, parsed, parent_id=None)


def create_variation(conn: Conn, profile_id: int, parent_id: int, created_by: str, title: str | None = None,
                     changes: list | None = None) -> int:
    """A new simulation decided on exactly the same frozen snapshot as `parent_id` (another of this supplier's), with its
    own hypothesis. The parent is not touched. The live inputs are read afresh: they are what drift is measured from."""
    title, created_by = _who(title, created_by)
    parsed = _checked(changes or [])
    with read_only(conn):
        parent = _row(conn, profile_id, parent_id)
        if parent["status"] == "discarded":
            raise SimulationConflict("a discarded simulation cannot be varied")
        snap = Snapshot.from_json(parent["snapshot"])
        if snap.sha256() != parent["snapshot_sha256"]:
            raise SimulationError("the stored snapshot does not match its hash")
        drift = {**{k: v for k, v in parent["drift"].items() if k != "live_inputs_at_creation"},
                 "live_inputs_at_creation": inputs.read(conn, snap)}
    apply_changes(snap, parsed)
    return _insert(conn, profile_id, snap, parent["snapshot_sha256"], drift, title, created_by, parsed, parent_id=parent_id, engine_version=parent["engine_version"])


def _who(title: str | None, created_by: str) -> tuple[str | None, str]:
    title = (title or "").strip() or None
    if title and len(title) > 500:
        raise SimulationError("keep the title under 500 characters")
    if not created_by.strip():
        raise SimulationError("say who created the simulation")
    return title, created_by.strip()


def _insert(conn: Conn, profile_id: int, snap: Snapshot, sha: str, drift: dict, title: str | None, created_by: str,
            changes: list[HypotheticalChange], parent_id: int | None, engine_version: str = ENGINE_VERSION) -> int:
    require_idle(conn)
    try:
        sid = must(conn.execute(
            """INSERT INTO simulation (profile_id, lot_id, baseline_assessment_id, version_id, title, as_of, snapshot,
                                       snapshot_sha256, drift, engine_version, created_by, parent_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (profile_id, snap.lot_id, snap.assessment_id, snap.version_id, title, snap.as_of, Jsonb(snap.to_json()),
             sha, Jsonb(drift), engine_version, created_by, parent_id)).fetchone())["id"]
        for ordinal, c in enumerate(changes, start=1):
            conn.execute("INSERT INTO simulation_change (simulation_id, ordinal, kind, payload) VALUES (%s, %s, %s, %s)",
                         (sid, ordinal, c.kind, Jsonb(dump_change(c))))
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
            raise SimulationConflict(f"changes can only be added before a simulation runs; this one is {row['status']}")
        parsed = _one(change)
        existing = _changes(conn, simulation_id)
        if len(existing) >= MAX_CHANGES:
            raise SimulationError(f"a simulation holds at most {MAX_CHANGES} changes")
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
            raise SimulationConflict("this simulation is already discarded" if status == "discarded" else
                                     f"a {status} simulation cannot be discarded until it finishes")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def get(conn: Conn, profile_id: int, simulation_id: int) -> dict:
    row = _row(conn, profile_id, simulation_id)
    return {**row, "changes": [dump_change(c) for c in _changes(conn, simulation_id)]}


def list_for_profile(conn: Conn, profile_id: int, include_discarded: bool = False, *, lot_id: int | None = None,
                     limit: int | None = None) -> list[dict]:
    """Newest first. `lot_id` narrows the list to one lot; it never widens it beyond this supplier."""
    return conn.execute(
        """SELECT s.id, s.lot_id, s.baseline_assessment_id, s.version_id, s.status, s.title, s.as_of, s.created_by, s.created_at,
                  s.parent_id, s.snapshot_sha256,
                  (SELECT count(*) FROM simulation_change c WHERE c.simulation_id = s.id) AS changes
           FROM simulation s WHERE s.profile_id = %s AND (%s OR s.status <> 'discarded') AND (%s::bigint IS NULL OR s.lot_id = %s)
           ORDER BY s.created_at DESC, s.id DESC LIMIT %s""",
        (profile_id, include_discarded, lot_id, lot_id, limit)).fetchall()


def scenario(conn: Conn, profile_id: int, simulation_id: int) -> Scenario:
    """The simulation's snapshot with its recorded changes applied, rebuilt from what is stored."""
    row = _row(conn, profile_id, simulation_id)
    return apply_changes(Snapshot.from_json(row["snapshot"]), _changes(conn, simulation_id))


def results(conn: Conn, profile_id: int, simulation_id: int) -> list[dict]:
    _row(conn, profile_id, simulation_id)
    return conn.execute("SELECT * FROM simulation_result WHERE simulation_id = %s ORDER BY requirement_id",
                        (simulation_id,)).fetchall()
