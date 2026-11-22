"""Baseline, control and simulated statuses side by side. Pure: no database.

The effect of a hypothesis is control -> simulated: both are computed by the same rules on the same frozen snapshot, so
the only difference between them is the changes. baseline -> control is drift (facts edited, a later date, verdicts the
model gave), and it is reported as drift, never credited to the hypothesis."""
from dataclasses import dataclass, field

STATUSES = ("met", "not_met", "needs_confirmation", "unknown")


@dataclass
class Row:
    requirement_id: int
    statement: str
    obligation: str
    baseline_verdict_id: int | None
    baseline_status: str | None
    control: dict | None    # the decision record, or None when only the model could decide it
    simulated: dict | None
    reached: list[dict] = field(default_factory=list)  # impact.Reason records: why a change can affect it
    notes: list[str] = field(default_factory=list)
    input_drift: list[dict] = field(default_factory=list)  # inputs.compare / since_baseline entries for this requirement

    attribution: dict | None = None

    @property
    def control_status(self) -> str | None:
        return self.control["status"] if self.control else None

    @property
    def simulated_status(self) -> str | None:
        return self.simulated["status"] if self.simulated else None

    @property
    def how(self) -> str:
        if not self.reached:
            return "not_affected"  # no change reaches it: its simulated verdict is its control verdict
        return "rule" if self.control and self.simulated else "needs_model"

    @property
    def effect(self) -> bool:
        return self.how == "rule" and self.control_status != self.simulated_status

    @property
    def drift(self) -> bool:
        return self.control is not None and self.baseline_status != self.control_status


def _counts(statuses: list[str | None]) -> dict:
    out = {s: 0 for s in STATUSES}
    out["needs_model"] = 0
    for s in statuses:
        out[s or "needs_model"] += 1
    return out


def summarise(rows: list[Row], baseline_recommendation: str | None, control_recommendation: str,
              simulated_recommendation: str, config: str, *, input_drift: dict | None = None,
              execution: dict | None = None) -> dict:
    """input_drift: {"since_creation": [...], "since_baseline": [...]} (inputs.py). execution: what the run read, and from where."""
    needs_model = [r.requirement_id for r in rows if r.how == "needs_model"]
    return {
        "config": config,
        "requirements": len(rows),
        "changed": [{"requirement_id": r.requirement_id, "statement": r.statement, "obligation": r.obligation,
                     "from": r.control_status, "to": r.simulated_status} for r in rows if r.effect],
        "drift": [{"requirement_id": r.requirement_id, "baseline": r.baseline_status, "control": r.control_status}
                  for r in rows if r.drift],
        # Live inputs that changed, reported even when no requirement's status moved (an override, for example, changes
        # only how a not-met verdict is aggregated into the recommendation).
        "input_drift": input_drift or {"since_creation": [], "since_baseline": []},
        "execution": execution,
        "needs_model": needs_model,
        "not_affected": [r.requirement_id for r in rows if r.how == "not_affected"],
        "stale_baseline": [r.requirement_id for r in rows if any(n.startswith("baseline may be out of date") for n in r.notes)],
        "counts": {"baseline": _counts([r.baseline_status for r in rows]),
                   "control": _counts([r.control_status for r in rows]),
                   "simulated": _counts([r.simulated_status for r in rows])},
        "recommendation": {
            "baseline": baseline_recommendation, "control": control_recommendation, "simulated": simulated_recommendation,
            # A requirement only the model can decide keeps its baseline verdict in both control and simulated, so the
            # recommendation compares like with like. That is exact when no change reaches it, so the recommendation is
            # complete unless a change reaches a requirement only the model could re-decide.
            "complete": not needs_model,
            # The recommendation moved before any hypothesis: drift, never part of the effect.
            "drift": baseline_recommendation is not None and baseline_recommendation != control_recommendation,
        },
    }
