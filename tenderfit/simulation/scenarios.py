"""Exploring scenarios (Mode B, phase 4): what a supplier can change, and how completed scenarios compare. Reads only.

A scenario is a simulation. Scenarios explored from one assessment either share one frozen snapshot exactly (a variation
copies its parent's, which 021's foreign key enforces) or were snapshotted separately, in which case their frozen inputs
may differ; the comparison says which.

Comparison semantics. A scenario's effect on a requirement is control -> simulated: the same frozen snapshot evaluated
without and with that scenario's changes, in one consistent read of the live inputs. The real assessment's verdict
(baseline) is shown beside it but never used to judge an effect, because baseline -> control also moves when live data
changes (drift). A transition is "improved" or "worsened" on this order of what a verdict means for eligibility:

    not_met  <  unknown  <  needs_confirmation  <  met

not_met on a mandatory requirement is a blocker; unknown means nothing proves it either way; needs_confirmation means
the evidence points to met but the supplier must confirm or prove it. Every transition is also counted by name (for
example not_met -> needs_confirmation), so the order never hides how far a verdict moved, and nothing is summed into a
score. A requirement only the model could decide is "undetermined", never guessed."""
from tenderfit.db import Conn
from tenderfit.profile.service import FACT_FIELDS, FACT_REQUIRED
from tenderfit.simulation import store
from tenderfit.simulation.changes import SimulationConflict, SimulationError, SimulationNotFound
from tenderfit.simulation.isolation import read_only
from tenderfit.simulation.scenario import EVIDENCE, NUMERIC
from tenderfit.simulation.snapshot import Snapshot, take_snapshot

RANK = {"not_met": 0, "unknown": 1, "needs_confirmation": 2, "met": 3}
VERDICT_ORDER = ["not_met", "unknown", "needs_confirmation", "met"]
MAX_COMPARED = 4
ORDERING = ("Scenarios are listed by mandatory blockers resolved (most first), then blockers introduced (fewest first), "
            "then uncertainties resolved (most first), then verdicts improved (most first) and worsened (fewest first). "
            "This is a sort over the counts shown, not a score, and it does not predict how the buyer will evaluate a bid.")
NOTICE_FIELDS = ("notice.deadline", "lot.quantity")
EDITABLE_EVIDENCE = ["document_on_file", "declared", "missing", "unknown", "needs_update"]
ADDED_EVIDENCE = ["declared", "document_on_file", "missing", "unknown"]


# --- What can be changed --------------------------------------------------------------------------------------------------

def inputs_for_assessment(conn: Conn, profile_id: int, assessment_id: int, lot_id: int) -> dict:
    """The frozen inputs a new scenario of this assessment would start from. Creates nothing."""
    with read_only(conn):
        snap, _ = take_snapshot(conn, profile_id, assessment_id)
    if snap.lot_id != lot_id:
        raise SimulationNotFound("assessment not found")
    return view(snap)


def inputs_for_simulation(conn: Conn, profile_id: int, simulation_id: int) -> dict:
    """An existing scenario's frozen inputs and hypothesis: what a variation of it starts from."""
    with read_only(conn):
        row = store.get(conn, profile_id, simulation_id)
    return {**view(Snapshot.from_json(row["snapshot"])), "simulation_id": simulation_id, "snapshot_sha256": row["snapshot_sha256"],
            "changes": row["changes"]}


def view(snap: Snapshot) -> dict:
    """The snapshot as a supplier builds changes from it. Every value here is actual (frozen from the live profile and the
    tender); nothing hypothetical is in it. `accepts` lists the change kinds each condition takes."""
    return {
        "assessment_id": snap.assessment_id, "lot_id": snap.lot_id, "as_of": snap.as_of.isoformat(),
        "requirements": [{
            "id": r["id"], "statement": r["statement"], "obligation": r["obligation"], "category": r["category"],
            "baseline_status": r["baseline_status"], "quote": r.get("quote"), "passage_id": r.get("passage_id"),
            "conditions": [_condition(c) for c in r["conditions"]],
        } for r in snap.requirements],
        "facts": [{"id": f.id, "attribute": f.attribute, "label": f.label, "value": f.value, "evidence_status": f.evidence_status,
                   "valid_until": f.valid_until.isoformat() if f.valid_until else None,
                   "editable_keys": sorted(FACT_FIELDS.get(f.attribute, {}))} for f in snap.facts],
        "fields": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in snap.fields.items() if k in NOTICE_FIELDS},
        "fact_kinds": {a: {"keys": {k: _type_name(t) for k, t in keys.items()}, "required": FACT_REQUIRED.get(a)}
                       for a, keys in FACT_FIELDS.items()},
        "evidence_statuses": {"existing": EDITABLE_EVIDENCE, "added": ADDED_EVIDENCE},
    }


def _type_name(t: type | tuple[type, ...]) -> str:
    if t is list:
        return "list"
    if t is bool:
        return "boolean"
    return "text" if t is str else "number"


def _condition(row: dict) -> dict:
    cond = row["condition"] or {}
    bound = cond.get("bound")
    accepts = []
    if cond.get("kind") in NUMERIC and isinstance(bound, (int, float)) and not isinstance(bound, bool):
        accepts.append("set_condition_bound")
    if cond.get("kind") in EVIDENCE:
        accepts.append("set_condition_match")
    return {"ordinal": row["ordinal"], "label": row.get("label"), "mandatory": row.get("mandatory", True),
            "machine_checkable": bool(cond), "kind": cond.get("kind"), "fact": cond.get("fact"), "bound": bound,
            "match": cond.get("match"), "unit": cond.get("unit"), "accepts": accepts}


# --- Comparing scenarios --------------------------------------------------------------------------------------------------

def direction(control: str | None, simulated: str | None, how: str) -> str:
    if how == "needs_model" or (how == "rule" and (control is None or simulated is None)):
        return "undetermined"
    if how == "not_affected" or control == simulated or control is None or simulated is None:
        return "unchanged"
    return "improved" if RANK[simulated] > RANK[control] else "worsened"


def compare(conn: Conn, profile_id: int, lot_id: int, ids: list[int]) -> dict:
    """Two to four of this supplier's completed scenarios of one assessment, requirement by requirement."""
    ids = list(dict.fromkeys(ids))
    if not 2 <= len(ids) <= MAX_COMPARED:
        raise SimulationError(f"choose 2 to {MAX_COMPARED} different scenarios to compare")
    with read_only(conn):
        sims = [store.get(conn, profile_id, sid) for sid in ids]  # another supplier's: SimulationNotFound
        if any(s["lot_id"] != lot_id for s in sims):
            raise SimulationNotFound("simulation not found")
        rows = {s["id"]: {r["requirement_id"]: r for r in store.results(conn, profile_id, s["id"])}
                for s in sims if s["status"] == "completed"}
    if not_done := [f"{s['id']} ({s['status']})" for s in sims if s["status"] != "completed"]:
        raise SimulationConflict(f"only completed scenarios can be compared; not completed: {', '.join(not_done)}")
    if len({s["baseline_assessment_id"] for s in sims}) > 1:
        raise SimulationConflict("these scenarios start from different assessments, so their results cannot be compared")

    snaps = {s["id"]: Snapshot.from_json(s["snapshot"]) for s in sims}
    scenarios = [_scenario(s, snaps[s["id"]], rows[s["id"]]) for s in sims]
    requirements = []
    for req in snaps[sims[0]["id"]].requirements:
        cells: dict[str, dict | None] = {str(s["id"]): _cell(rows[s["id"]].get(req["id"])) for s in sims}
        present = [c for c in cells.values() if c]
        requirements.append({
            "requirement_id": req["id"], "statement": req["statement"], "obligation": req["obligation"],
            "category": req["category"], "baseline_status": req["baseline_status"], "quote": req.get("quote"),
            "passage_id": req.get("passage_id"), "scenarios": cells,
            "affected": any(c["direction"] != "unchanged" for c in present),
            "differs_between_scenarios": len({(c["control"], c["simulated"], c["how"]) for c in present}) > 1,
        })
    order = sorted(scenarios, key=lambda x: (-len(x["highlights"]["blockers_resolved"]), len(x["highlights"]["blockers_introduced"]),
                                             -len(x["highlights"]["uncertainties_resolved"]), -x["counts"]["improved"],
                                             x["counts"]["worsened"], x["id"]))
    return {"hypothetical": True, "lot_id": lot_id, "baseline_assessment_id": sims[0]["baseline_assessment_id"],
            "integrity": _integrity(sims), "scenarios": scenarios, "requirements": requirements,
            "facts": {str(f.id): f.label for snap in snaps.values() for f in snap.facts},  # to name the facts changes touch
            "order": [x["id"] for x in order], "ordering_rule": ORDERING, "verdict_order": VERDICT_ORDER,
            "priorities": priorities(scenarios)}


def _cell(r: dict | None) -> dict | None:
    if r is None:
        return None
    sim = r["simulated"] or {}
    return {"control": r["control_status"], "simulated": r["simulated_status"], "how": r["how"],
            "direction": direction(r["control_status"], r["simulated_status"], r["how"]), "effect": sim.get("effect", False),
            "reason_control": (sim.get("control") or {}).get("reason"),
            "reason_simulated": (sim.get("simulated") or {}).get("reason"),
            "reached": sim.get("reached", []), "notes": sim.get("notes", []), "drift": sim.get("drift", False),
            "input_drift": sim.get("input_drift", [])}


def _scenario(sim: dict, snap: Snapshot, rows: dict[int, dict]) -> dict:
    mandatory = {r["id"] for r in snap.requirements if r["obligation"] == "mandatory"}
    counts = {"improved": 0, "worsened": 0, "unchanged": 0, "undetermined": 0}
    transitions: dict[str, int] = {}
    hl: dict[str, list[int]] = {"blockers_resolved": [], "blockers_now_unknown": [], "blockers_introduced": [],
                                "uncertainties_resolved": [], "now_needs_confirmation": [], "needs_model": []}
    for rid, r in sorted(rows.items()):
        c, s, how = r["control_status"], r["simulated_status"], r["how"]
        d = direction(c, s, how)
        counts[d] += 1
        if d in ("improved", "worsened"):
            transitions[f"{c} -> {s}"] = transitions.get(f"{c} -> {s}", 0) + 1
        if how == "needs_model":
            hl["needs_model"].append(rid)
        if how != "rule" or s is None or c is None:
            continue
        if rid in mandatory and c == "not_met" and s in ("met", "needs_confirmation"):
            hl["blockers_resolved"].append(rid)
        if rid in mandatory and c == "not_met" and s == "unknown":
            hl["blockers_now_unknown"].append(rid)  # no longer contradicted, but nothing proves it either: not resolved
        if rid in mandatory and s == "not_met" and c != "not_met":
            hl["blockers_introduced"].append(rid)
        if c == "unknown" and s != "unknown":
            hl["uncertainties_resolved"].append(rid)
        if s == "needs_confirmation" and c != "needs_confirmation":
            hl["now_needs_confirmation"].append(rid)
    summary = sim["summary"] or {}
    return {"id": sim["id"], "title": sim["title"], "parent_id": sim.get("parent_id"), "as_of": sim["as_of"],
            "snapshot_sha256": sim["snapshot_sha256"], "changes": reach_by_change(sim["changes"], rows, hl, snap),
            "recommendation": summary.get("recommendation"), "counts": counts, "transitions": transitions,
            "highlights": hl, "no_effect": counts["improved"] + counts["worsened"] == 0,
            "input_drift": summary.get("input_drift") or {"since_creation": [], "since_baseline": []},
            "drift": [d["requirement_id"] for d in summary.get("drift", [])]}


def target(change: dict, added: int) -> str:
    """What a change touches, in impact.Reason's terms. `added` is this add_fact's position among added facts (1-based):
    scenario.apply_changes numbers added facts -1, -2, ... in order."""
    kind = change["kind"]
    if kind in ("set_condition_bound", "set_condition_match", "set_requirement_text"):
        return f"requirement:{change['requirement_id']}"
    if kind == "add_fact":
        return f"fact:{-added}"
    if kind == "set_notice_field":
        return f"field:{change['field']}"
    return f"fact:{change['fact_id']}"


ASSUMES_EVIDENCE = ("document_on_file", "declared")


def assumes_evidence(change: dict, snap: Snapshot | None = None) -> bool:
    """Whether the change supposes evidence or a fact the supplier does not hold today: an added fact, an existing one
    treated as better evidenced, or one valid for longer than its frozen expiry date. Its effect depends on the supplier
    actually obtaining that evidence."""
    if change["kind"] == "add_fact":
        return True
    if change["kind"] == "set_fact_evidence":
        return change["evidence_status"] in ASSUMES_EVIDENCE
    if change["kind"] == "set_fact_validity":
        f = next((f for f in snap.facts if f.id == change["fact_id"]), None) if snap else None
        new = change["valid_until"]
        return f is None or new is None or (f.valid_until is not None and str(new) > f.valid_until.isoformat())
    return False


def reach_by_change(changes: list[dict], rows: dict[int, dict], highlights: dict[str, list[int]] | None = None,
                    snap: Snapshot | None = None) -> list[dict]:
    """Which requirements each change reached, from the dependency reasons the run recorded (impact.reach), and which of
    those moved. A moved requirement is `attributable` to a change only when no other change of the scenario reached it;
    otherwise the changes share it, and nothing here says which of them caused the move."""
    hl = highlights or {}
    targets, added = [], 0
    for change in changes:
        added += change["kind"] == "add_fact"
        targets.append(target(change, added))
    reached = [sorted(rid for rid, r in rows.items() if any(x.get("target") == t for x in (r["simulated"] or {}).get("reached", [])))
               for t in targets]
    out = []
    for n, (change, t, reach) in enumerate(zip(changes, targets, reached, strict=True), start=1):
        others = {rid for i, rs in enumerate(reached) if i != n - 1 for rid in rs}
        moved = [rid for rid in reach
                 if direction(rows[rid]["control_status"], rows[rid]["simulated_status"], rows[rid]["how"]) in ("improved", "worsened")]
        out.append({
            "ordinal": n, "change": change, "target": t, "reaches": reach, "moved": moved,
            "attributable": [rid for rid in moved if rid not in others], "shared_with_other_changes": sorted(set(moved) & others),
            "undetermined": [rid for rid in reach if rows[rid]["how"] == "needs_model"],
            **{k: [rid for rid in moved if rid in hl.get(k, [])] for k in
               ("blockers_resolved", "blockers_introduced", "uncertainties_resolved")},
            "no_effect": not moved, "assumes_evidence": assumes_evidence(change, snap),
        })
    return out


def priorities(scenarios: list[dict]) -> dict:
    """Changes across the compared scenarios, grouped by what the dependency data shows they did. Deterministic; each
    list is ordered by how many requirements the change moved, then scenario and ordinal. Not a forecast."""
    every = [{"scenario_id": s["id"], **{k: c[k] for k in ("ordinal", "change", "reaches", "moved", "attributable",
                                                           "blockers_resolved", "blockers_introduced", "uncertainties_resolved",
                                                           "assumes_evidence")}}
             for s in scenarios for c in s["changes"]]
    key = lambda c: (-len(c["moved"]), -len(c["reaches"]), c["scenario_id"], c["ordinal"])  # noqa: E731
    return {
        "widest_reach": sorted((c for c in every if c["reaches"]), key=lambda c: (-len(c["reaches"]), c["scenario_id"], c["ordinal"])),
        "addresses_blockers": sorted((c for c in every if c["blockers_resolved"]), key=key),
        "resolves_uncertainty": sorted((c for c in every if c["uncertainties_resolved"]), key=key),
        "introduces_blockers": sorted((c for c in every if c["blockers_introduced"]), key=key),
        "little_or_no_effect": sorted((c for c in every if not c["moved"]), key=key),
        "depends_on_evidence_not_held": sorted((c for c in every if c["assumes_evidence"] and c["moved"]), key=key),
        "basis": ("Grouped from the requirements each change reached in the dependency analysis and the verdicts that moved. "
                  "A move shared by several changes of one scenario is listed under each of them, and is not attributed to "
                  "any one. None of this predicts the outcome of a bid."),
    }


def _integrity(sims: list[dict]) -> dict:
    """Whether the scenarios were decided on the same inputs. Every difference is named, never smoothed over."""
    notes = []
    same_snapshot = len({s["snapshot_sha256"] for s in sims}) == 1
    if not same_snapshot:
        notes.append("These scenarios were frozen separately, so their supplier facts, requirements or tender fields may "
                     "differ. Variations of one scenario share its frozen inputs exactly.")
    if len({s["as_of"] for s in sims}) > 1:
        notes.append("These scenarios judge dates as of different days.")
    if len({s["engine_version"] for s in sims}) > 1:
        notes.append("These scenarios were run by different versions of the rule engine.")
    at_run: dict[str, set] = {}
    for s in sims:
        for name, d in (((s["summary"] or {}).get("execution") or {}).get("live_inputs") or {}).items():
            at_run.setdefault(name, set()).add(d.get("at_run"))
    moved = sorted(k for k, v in at_run.items() if len(v) > 1)
    if moved:
        notes.append(f"Live inputs changed between these scenarios' runs ({', '.join(moved)}), so their 'without changes' "
                     "verdicts may differ for that reason rather than because of the hypotheses.")
    drifted = [s["id"] for s in sims if ((s["summary"] or {}).get("input_drift") or {}).get("since_creation")]
    if drifted:
        notes.append(f"Live inputs changed between creation and run for scenario {', '.join(map(str, drifted))}.")
    return {"same_snapshot": same_snapshot, "same_live_inputs": not moved, "consistent": not notes, "notes": notes}
