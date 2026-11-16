"""Hypothetical changes applied to a snapshot, in memory. No database, no I/O: the same snapshot and changes always give
the same scenario, and the snapshot itself is never modified.

Every change is checked here against the state it is applied to, in order, so a later change can refer to a fact an
earlier one added. `touched` records what each change reaches, as the dependency targets changes.impact understands."""
import copy
from dataclasses import dataclass, field, replace
from datetime import date

from pydantic import ValidationError

from tenderfit.profile.service import FACT_FIELDS, ProfileError, clean_fact
from tenderfit.profile.validity import default_confirm_by
from tenderfit.rules.engine import Condition, Fact
from tenderfit.simulation.changes import (
    AddFact,
    HypotheticalChange,
    RemoveFact,
    SetConditionBound,
    SetConditionMatch,
    SetFactEvidence,
    SetFactValidity,
    SetFactValue,
    SetNoticeField,
    SetRequirementText,
    SimulationError,
    parse_change,
)
from tenderfit.simulation.snapshot import Snapshot

NUMERIC = ("min_amount", "at_least", "at_most")
EVIDENCE = ("has_evidence", "valid_through")


@dataclass
class Scenario:
    snapshot: Snapshot
    facts: list[Fact]
    fields: dict
    requirements: dict[int, dict]
    changes: list[HypotheticalChange] = field(default_factory=list)
    touched: set[tuple[str, str]] = field(default_factory=set)  # ("fact", id), ("field", path), ("requirement", id)

    @property
    def as_of(self) -> date:
        return self.snapshot.as_of


def apply_changes(snapshot: Snapshot, changes: list) -> Scenario:
    s = Scenario(snapshot, [replace(f, value=copy.deepcopy(f.value)) for f in snapshot.facts], dict(snapshot.fields),
                 {r["id"]: copy.deepcopy(r) for r in snapshot.requirements})
    for n, raw in enumerate(changes, start=1):
        change = parse_change(raw)
        try:
            _apply(s, change)
        except SimulationError as e:
            raise SimulationError(f"change {n} ({change.kind}): {e}") from None
        s.changes.append(change)
    return s


def _requirement(s: Scenario, rid: int) -> dict:
    req = s.requirements.get(rid)
    if req is None:
        raise SimulationError(f"requirement {rid} is not part of the baseline assessment")
    return req


def _condition(s: Scenario, rid: int, ordinal: int, kinds: tuple[str, ...]) -> dict:
    req = _requirement(s, rid)
    row = next((c for c in req["conditions"] if c["ordinal"] == ordinal), None)
    if row is None:
        raise SimulationError(f"requirement {rid} has no condition {ordinal}")
    if row["condition"] is None:
        raise SimulationError(f"condition {ordinal} of requirement {rid} is not machine-checkable: the model reads it, so it has "
                              "no parameter to change")
    try:
        kind = Condition(**row["condition"]).kind
    except ValidationError as e:
        raise SimulationError(f"condition {ordinal} of requirement {rid} could not be read") from e
    if kind not in kinds:
        raise SimulationError(f"condition {ordinal} of requirement {rid} is a {kind} check; this change applies to {', '.join(kinds)}")
    return row


def _fact(s: Scenario, fact_id: int) -> Fact:
    f = next((f for f in s.facts if f.id == fact_id), None)
    if f is None:
        raise SimulationError(f"fact {fact_id} is not in this supplier's profile snapshot")
    return f


def _swap(s: Scenario, old: Fact, new: Fact) -> None:
    s.facts[s.facts.index(old)] = new
    s.touched.add(("fact", str(old.id)))


def _clean(attribute: str, label: str, value: dict) -> tuple[str, dict]:
    if attribute not in FACT_FIELDS:
        raise SimulationError(f"unknown kind of fact: {attribute}")
    try:
        return clean_fact(attribute, label, value)
    except ProfileError as e:
        raise SimulationError(str(e)) from None


def _apply(s: Scenario, c: HypotheticalChange) -> None:
    if isinstance(c, SetConditionBound):
        row = _condition(s, c.requirement_id, c.condition, NUMERIC)
        row["condition"] = {**row["condition"], "bound": c.bound}
        s.touched.add(("requirement", str(c.requirement_id)))
    elif isinstance(c, SetConditionMatch):
        row = _condition(s, c.requirement_id, c.condition, EVIDENCE)
        row["condition"] = {**row["condition"], "match": c.match.strip()}
        s.touched.add(("requirement", str(c.requirement_id)))
    elif isinstance(c, SetFactValidity):
        f = _fact(s, c.fact_id)
        _swap(s, f, replace(f, valid_until=c.valid_until))
    elif isinstance(c, SetFactEvidence):
        f = _fact(s, c.fact_id)
        _swap(s, f, replace(f, evidence_status=c.evidence_status))
    elif isinstance(c, RemoveFact):
        f = _fact(s, c.fact_id)
        s.facts.remove(f)
        s.touched.add(("fact", str(f.id)))
    elif isinstance(c, SetFactValue):
        f = _fact(s, c.fact_id)
        _, value = _clean(f.attribute, f.label, {**f.value, c.key: c.value})
        if c.key not in value:
            raise SimulationError(f"{c.key} cannot be set to an empty value; remove the fact instead")
        _swap(s, f, replace(f, value=value))
    elif isinstance(c, AddFact):
        label, value = _clean(c.attribute, c.label, c.value)
        new_id = -1 - sum(isinstance(x, AddFact) for x in s.changes)
        # As profile.service.add_declared_fact does: what the supplier states is confirmed on the day it is stated.
        confirmed = s.as_of if c.evidence_status in ("declared", "document_on_file") else None
        s.facts.append(Fact(new_id, c.attribute, label, value, c.evidence_status, c.valid_until,
                            default_confirm_by(c.attribute, confirmed), confirmed))
        s.touched.add(("fact", str(new_id)))
    elif isinstance(c, SetNoticeField):
        s.fields[c.field] = float(c.value) if c.field == "lot.quantity" else c.value  # type: ignore[arg-type]
        s.touched.add(("field", c.field))
    elif isinstance(c, SetRequirementText):
        req = _requirement(s, c.requirement_id)
        req.update({k: v for k, v in (("statement", c.statement), ("quote", c.quote)) if v is not None}, text_changed=True)
        s.touched.add(("requirement", str(c.requirement_id)))
    else:  # pragma: no cover - the union is closed; a new kind must be handled here before it can be parsed
        raise SimulationError(f"unhandled change {c.kind}")
