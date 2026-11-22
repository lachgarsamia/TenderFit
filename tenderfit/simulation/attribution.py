"""Bounded counterfactual rule checks, evaluated on the same transaction as the main simulation.

Labels are overlapping structural properties, not causal claims or probabilities. In particular a change may be both
sufficient alone and redundant with the rest. No subset is reordered or repaired: dependent edits may be untestable.
"""
from collections.abc import Callable

from tenderfit.simulation import provenance
from tenderfit.simulation.changes import SimulationError
from tenderfit.simulation.compare import Row
from tenderfit.simulation.scenario import Scenario, apply_changes
from tenderfit.simulation.scenarios import target
from tenderfit.simulation.snapshot import Snapshot

MAX_DECISIONS = 200
BASIS = ("For moved requirements reached by multiple changes, each computed pair checks a change alone and all changes "
         "except it, using rules on this frozen copy in the same transaction. Limits and unavailable alternatives are explicit. "
         "Labels describe these checked alternatives, not causality or a prediction of the buyer's decision. "
         "Change order is preserved; invalid dependent subsets are not repaired.")


def classify(control: str, final: str, alone: str, without: str) -> list[str]:
    labels = []
    if alone == final:
        labels.append("sufficient_alone")
    if without != final:
        labels.append("necessary")
        if alone != final:
            labels.append("jointly_required")
    else:
        labels.append("redundant")
    if alone == control and without == final:
        labels.append("no_independent_effect")
    return labels


def analyze(rows: list[Row], snapshot: Snapshot, changes: list[dict],
            decide: Callable[[Scenario, int], dict | None], *, limit: int = MAX_DECISIONS) -> dict:
    """Mutates only the in-memory rows. At most `limit` extra decide calls across the entire simulation."""
    limit = max(0, limit)
    calls = 0
    targets, added = [], 0
    for change in changes:
        added += change["kind"] == "add_fact"
        targets.append(target(change, added))
    all_ordinals = set(range(1, len(changes) + 1))

    def probe(rid: int, selected: set[int]) -> dict:
        nonlocal calls
        try:
            state = apply_changes(snapshot, changes, include=selected)
        except SimulationError as e:
            return {"state": "invalid_subset", "detail": str(e), "ordinals": sorted(selected)}
        calls += 1
        decision = decide(state, rid)
        if decision is None:
            return {"state": "needs_model", "ordinals": sorted(selected)}
        return {"state": "decided", "ordinals": sorted(selected), "status": decision["status"],
                "reason": decision["reason"], "provenance": provenance.record(decision, state, state, rid)}

    for row in sorted(rows, key=lambda r: r.requirement_id):
        if not row.effect:
            continue
        control, final = row.control_status, row.simulated_status
        assert control is not None and final is not None  # row.effect only includes decided rule transitions
        reached = {r["target"] for r in row.reached}
        ordinals = [i for i, t in enumerate(targets, 1) if t in reached]
        entries = []
        for ordinal in ordinals:
            if len(ordinals) == 1:
                # Existing dependency attribution: no extra counterfactual is needed or claimed.
                entries.append({"ordinal": ordinal, "state": "single_reaching_change", "labels": [],
                                "detail": "Only this change reaches the moved requirement; no extra alternatives were evaluated."})
                continue
            if calls + 2 > limit:
                entries.append({"ordinal": ordinal, "state": "limit", "labels": [],
                                "detail": "Not computed: the extra rule-evaluation limit was reached."})
                continue
            alone = probe(row.requirement_id, {ordinal})
            without = probe(row.requirement_id, all_ordinals - {ordinal})
            complete = alone["state"] == without["state"] == "decided"
            entries.append({"ordinal": ordinal, "state": "computed" if complete else "undetermined",
                            "labels": classify(control, final, alone["status"], without["status"]) if complete else [],
                            "alone": alone, "without": without})
        row.attribution = {"basis": BASIS, "changes": entries}
    return {"basis": BASIS, "decision_limit": limit, "decisions": calls,
            "limited": any(e["state"] == "limit" for r in rows for e in (r.attribution or {}).get("changes", []))}
