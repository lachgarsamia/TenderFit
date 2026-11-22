"""Evidence and assumptions behind a rules-only result. Pure; never refresh frozen evidence from live data."""
from datetime import date

from tenderfit.simulation.changes import dump_change
from tenderfit.simulation.scenario import Scenario


def _value(value):
    return value.isoformat() if isinstance(value, date) else value


def record(decision: dict, state: Scenario, hypothesis: Scenario, requirement_id: int, *, control: bool = False) -> dict:
    facts = []
    remaining = {f.id for f in hypothesis.facts}
    for f in state.facts:
        if f.id not in decision["facts"]:
            continue
        changes = hypothesis.fact_changes.get(f.id, [])
        origin = ("removed" if f.id not in remaining else "actual") if control else (
            "hypothetical" if f.id < 0 else "modified" if changes else "actual")
        facts.append({"id": f.id, "label": f.label, "origin": origin, "value": f.value,
                      "evidence_status": f.evidence_status, "valid_until": _value(f.valid_until),
                      "declaration_confirmed": f.declaration_confirmed,
                      "changes": changes if origin != "actual" else [],
                      # These support the original fact, never the assumed replacement.
                      "support": (state.snapshot.support or {}).get(f.id, []),
                      "support_available": state.snapshot.support is not None})
    params = []
    for field in decision["params"]:
        changes = []
        previous = _value(state.snapshot.fields.get(field))
        for i, c in zip(hypothesis.change_ordinals, hypothesis.changes, strict=True):
            if c.kind == "set_notice_field" and c.field == field:
                after = _value(c.value)
                changes.append({"ordinal": i, "before": previous, "after": after})
                previous = after
        params.append({"field": field, "value": _value(state.fields.get(field)),
                       "origin": "modified" if changes and not control else "actual", "changes": changes if not control else []})
    assumptions = [{"ordinal": i, "change": dump_change(c)} for i, c in zip(hypothesis.change_ordinals, hypothesis.changes, strict=True)
                   if getattr(c, "requirement_id", None) == requirement_id] if not control else []
    decision_inputs = None
    if state.answers is not None:
        decision_inputs = {"answers": state.answers.get(requirement_id, []),
                           "overridden": (state.overridden or {}).get(requirement_id, False),
                           "origin": "modified" if not control and any(c.kind in ("set_requirement_answer", "set_override") and getattr(c, "requirement_id", None) == requirement_id for c in hypothesis.changes) else "actual"}
    return {"decision_inputs": decision_inputs, "citations": decision["citations"], "facts": facts, "params": params,
            "requirement_assumptions": assumptions, "needs_human": decision["needs_human"],
            "gaps": decision["gaps"], "support_available": state.snapshot.support is not None}
