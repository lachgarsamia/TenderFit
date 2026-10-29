from dataclasses import dataclass, field

import psycopg

from tenderfit.changes.diff import Change


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


def load_deps(conn: psycopg.Connection, version_id: int) -> list[Dep]:
    return [Dep(r["requirement_id"], r["kind"], r["target_type"], r["target_id"]) for r in conn.execute(
        "SELECT DISTINCT requirement_id, kind, target_type, target_id FROM dependency WHERE version_id = %s", (version_id,))]
