"""Which requirements a hypothesis can reach, and why. Pure: no database.

Two sources of reach, both reported:
  used      the baseline's recorded dependencies (changes.impact.load_deps): this verdict used that fact or field.
            They explain an impact in the supplier's terms, but alone they are not enough: a verdict records only the
            facts it used, so a fact it could not find (or found but set aside) leaves no record, and adding or editing
            one would be missed.
  examines  what the rule engine can read, derived from the conditions themselves: a condition on `capacity.lead_days`
            selects among every capacity fact (rules.engine._select), resolves any `lot.` / `notice.` field its
            parameters name (_resolve), and measures every fact's validity at the deadline (profile.validity.check).
            A requirement only the model can read sees the facts relevant to its category (pipeline.relevant_facts) and
            the deadline.
A requirement no change reaches by either route cannot change, so its simulated verdict is its control verdict. Running
with selective=False recomputes everything and fails loudly if that ever turns out false."""
from dataclasses import dataclass

from tenderfit.assessment.pipeline import CATEGORY_FACTS
from tenderfit.changes.impact import Dep
from tenderfit.rules.engine import Fact

FIELD_PARAMS = ("bound", "anchor", "min_units")
DEFAULT_FIELDS = {"region_covered": "lot.regions", "within_years": "notice.deadline"}


@dataclass(frozen=True)
class Reason:
    kind: str     # "direct" | "used" | "examines"
    target: str   # "fact:12", "field:notice.deadline", "requirement:3"
    detail: str

    def json(self) -> dict:
        return {"kind": self.kind, "target": self.target, "detail": self.detail}


def reads(req: dict) -> tuple[set[str], set[str], bool]:
    """(fact attributes, fields, needs the model) a requirement's decision can read."""
    attributes, fields, model = set(), set(), False
    for row in req["conditions"]:
        cond = row["condition"]
        if not cond:
            model = True
            continue
        attributes.add(str(cond.get("fact", "")).split(".")[0])
        fields.add("notice.deadline")  # validity of every selected fact is measured at the deadline
        if cond.get("kind") in DEFAULT_FIELDS:
            fields.add(DEFAULT_FIELDS[cond["kind"]])
        for k in FIELD_PARAMS:
            v = cond.get(k)
            if isinstance(v, str) and v.split(".")[0] in ("notice", "lot"):
                fields.add(v)
    if model:
        want = CATEGORY_FACTS.get(req["category"])
        attributes |= {"*"} if want is None else want
        fields.add("notice.deadline")  # the model is told the deadline
    return attributes, fields, model


def reach(requirements: list[dict], touched: set[tuple[str, str]], deps: list[Dep], facts: list[Fact]) -> dict[int, list[Reason]]:
    """For each requirement, every reason a touched target reaches it. `facts` must include every fact a target can name:
    the snapshot's (for removed and edited facts) and the scenario's (for added ones)."""
    attr = {str(f.id): f for f in facts}
    by_req: dict[int, list[Dep]] = {}
    for d in deps:
        by_req.setdefault(d.requirement_id, []).append(d)
    out: dict[int, list[Reason]] = {}
    for req in requirements:
        rid = req["id"]
        reasons: list[Reason] = []
        if ("requirement", str(rid)) in touched:
            reasons.append(Reason("direct", f"requirement:{rid}", "the hypothesis changes this requirement's own conditions or wording"))
        recorded = {(d.target_type, d.target_id) for d in by_req.get(rid, [])}
        attributes, fields, _ = reads(req)
        for kind, target in sorted(touched):
            if kind == "requirement":
                continue
            if (kind, target) in recorded:
                what = f"fact {_name(attr, target)}" if kind == "fact" else target
                reasons.append(Reason("used", f"{kind}:{target}", f"the baseline verdict used {what}"))
            elif kind == "fact" and target in attr and ({"*", attr[target].attribute} & attributes):
                reasons.append(Reason("examines", f"fact:{target}",
                                      f"its decision reads every {attr[target].attribute} fact, and {_name(attr, target)} is one"))
            elif kind == "field" and target in fields:
                reasons.append(Reason("examines", f"field:{target}", f"its decision reads {target}"))
        out[rid] = reasons
    return out


def _name(facts: dict[str, Fact], fid: str) -> str:
    f = facts.get(fid)
    return f"{fid} ({f.label})" if f else fid
