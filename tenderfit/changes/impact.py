from dataclasses import dataclass, field

from tenderfit.changes.diff import Change
from tenderfit.db import Conn


@dataclass
class Dep:
    requirement_id: int
    kind: str
    target_type: str
    target_id: str


@dataclass
class Plan:
    mode: str
    reason: str | None = None
    rules_only: set[int] = field(default_factory=set)
    reassess: set[int] = field(default_factory=set)
    reextract: set[int] = field(default_factory=set)
    hits: list[Dep] = field(default_factory=list)


def changed_targets(change: Change, facts: set[int] | None = None, documents: set[int] | None = None) -> set[tuple[str, str]]:
    out = {("passage", str(p)) for p in change.changed_passages}
    out |= {("field", f.path.split("@")[0]) for f in change.fields}
    out |= {("document", str(d)) for d in (documents or set())}
    out |= {("fact", str(f)) for f in (facts or set())}
    return out


def plan_reassessment(change: Change, deps: list[Dep], facts: set[int] | None = None,
                      documents: set[int] | None = None) -> Plan:
    if change.cls in ("structural", "ambiguous"):
        return Plan("full", reason="; ".join(change.reasons) or change.cls)
    changed = changed_targets(change, facts, documents)
    hits = [d for d in deps if (d.target_type, d.target_id) in changed]
    affected = {d.requirement_id for d in hits}
    model_needed = {d.requirement_id for d in hits if d.kind != "parameter"}
    return Plan("targeted", rules_only=affected - model_needed, reassess=model_needed,
                reextract=change.new_passage_ids, hits=hits)


def load_deps(conn: Conn, assessment_id: int, static_only: bool = False) -> list[Dep]:
    """What one assessment's verdicts depend on: what extraction recorded for its requirements, plus what those verdicts
    actually read. Another supplier's verdicts, and earlier or shadow runs, have no say in this supplier's plan.
    static_only leaves out what the verdicts read, for comparing the two approaches in evaluation."""
    return [Dep(r["requirement_id"], r["kind"], r["target_type"], r["target_id"]) for r in conn.execute(
        """SELECT DISTINCT d.requirement_id, d.kind, d.target_type, d.target_id FROM dependency d
           JOIN verdict v ON v.assessment_id = %s AND v.requirement_id = d.requirement_id
           WHERE d.verdict_id IS NULL OR (d.verdict_id = v.id AND NOT %s)""", (assessment_id, static_only))]
