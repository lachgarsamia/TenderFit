from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel

from tenderfit.profile.validity import FactView, check

MET, NOT_MET, NEEDS, UNKNOWN = "met", "not_met", "needs_confirmation", "unknown"
PROOF_CATEGORIES = {"certificate", "insurance", "standard", "experience"}
WORKLOAD_PATHS = {"capacity.lead_days", "workload", "staff", "installation_slots"}


class Condition(BaseModel):
    kind: Literal["at_least", "at_most", "within_years", "region_covered", "has_evidence", "min_amount"]
    fact: str
    bound: float | str | None = None
    years: int | None = None
    anchor: str | None = None
    min_units: float | str | None = None
    match: str | None = None


@dataclass
class Fact:
    id: int
    attribute: str
    label: str
    value: dict
    evidence_status: str
    valid_until: date | None = None
    confirm_by: date | None = None
    confirmed_at: date | None = None
    confirmed_for_lot: bool = False

    def view(self) -> FactView:
        return FactView(self.attribute, self.evidence_status, self.valid_until, self.confirm_by, self.confirmed_at)


@dataclass
class Context:
    today: date
    fields: dict  # "notice.deadline", "lot.quantity", "lot.regions", ...

    @property
    def deadline(self) -> date | None:
        d = self.fields.get("notice.deadline")
        return d if isinstance(d, date) or d is None else None


@dataclass
class RuleResult:
    status: str
    reason: str
    basis: str = "rule"
    facts: list[int] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    question: str | None = None
    needs_human: str | None = None


def _resolve(v, ctx: Context, params: list[str]):
    if isinstance(v, str) and v.split(".")[0] in ("notice", "lot"):
        params.append(v)
        return ctx.fields.get(v)
    return v


def _num(fact: Fact, path: str):
    key = path.split(".", 1)[1] if "." in path else None
    v = fact.value.get(key) if key else None
    return v if isinstance(v, (int, float)) else None


def _candidates(facts: list[Fact], path: str, match: str | None = None) -> list[Fact]:
    attr = path.split(".")[0]
    out = [f for f in facts if f.attribute == attr]
    if match:
        m = match.lower().replace(" ", "")
        out = [f for f in out if m in str(f.value).lower().replace(" ", "") or m in f.label.lower().replace(" ", "")]
    return out


def _settle(ok: bool, fact: Fact, ctx: Context, params: list[str], what: str, proof_needed: bool,
            workload: bool = False) -> RuleResult:
    v = check(fact.view(), today=ctx.today, deadline=ctx.deadline)
    if not ok:
        if v.usable and not v.stale:
            return RuleResult(NOT_MET, f"{what}: contrary evidence ({fact.label})", facts=[fact.id], params=params,
                              needs_human="G6")
        return RuleResult(NEEDS, f"{what}: the fact that contradicts it is out of date ({v.reason})", facts=[fact.id],
                          params=params, question=f"Please update: {fact.label}", needs_human="G4")
    if not v.usable:
        return RuleResult(NEEDS, f"{what}: {v.reason}", facts=[fact.id], params=params,
                          question=v.question or f"Please update: {fact.label}", needs_human="G4")
    if workload and not fact.confirmed_for_lot:
        return RuleResult(NEEDS, f"{what} on declared facts; workload not confirmed for this lot", basis="declared",
                          facts=[fact.id], params=params, question=f"Can you confirm '{fact.label}' for this order?",
                          needs_human="G3")
    if v.stale:
        return RuleResult(NEEDS, f"{what}: {v.reason}", facts=[fact.id], params=params, question=v.question,
                          needs_human="G4")
    if proof_needed and fact.evidence_status == "declared":
        return RuleResult(NEEDS, f"{what} on declared facts; proof is required", basis="declared", facts=[fact.id],
                          params=params, question=f"Upload evidence for '{fact.label}' or confirm the declaration.",
                          needs_human="G2")
    basis = "documents" if fact.evidence_status == "document_on_file" else "declared"
    return RuleResult(MET, f"{what} ({'on documents' if basis == 'documents' else 'on declared facts'})", basis=basis,
                      facts=[fact.id], params=params)


def evaluate(cond: Condition, facts: list[Fact], ctx: Context, *, category: str = "other",
             proof_needed: bool = False) -> RuleResult:
    params: list[str] = []
    proof = proof_needed or category in PROOF_CATEGORIES
    cands = _candidates(facts, cond.fact, cond.match)
    if not cands:
        return RuleResult(UNKNOWN, f"no {cond.fact.split('.')[0]} fact in the profile", params=params,
                          question=f"Do you have {cond.match or cond.fact.split('.')[0]}?")
    known = [f for f in cands if f.evidence_status not in ("missing", "unknown")]
    if not known:
        f = cands[0]
        return RuleResult(UNKNOWN, f"{f.label}: evidence {f.evidence_status}, not proven either way", facts=[f.id],
                          params=params, question=f"Do you hold {f.label}?")

    if cond.kind in ("at_least", "at_most"):
        bound = _resolve(cond.bound, ctx, params)
        if not isinstance(bound, (int, float)):
            return RuleResult(UNKNOWN, f"bound {cond.bound} not available", params=params)
        best = None
        for f in known:
            v = _num(f, cond.fact)
            if v is None:
                continue
            ok = v >= bound if cond.kind == "at_least" else v <= bound
            if best is None or ok:
                best = (ok, f, v)
            if ok:
                break
        if best is None:
            return RuleResult(UNKNOWN, f"profile has no value for {cond.fact}", facts=[f.id for f in known], params=params)
        ok, f, v = best
        op = ">=" if cond.kind == "at_least" else "<="
        return _settle(ok, f, ctx, params, f"{cond.fact} {v:g} {op} {bound:g}", proof,
                       workload=cond.fact in WORKLOAD_PATHS or cond.fact.split('.')[0] in WORKLOAD_PATHS)

    if cond.kind == "within_years":
        anchor = _resolve(cond.anchor or "notice.deadline", ctx, params) or ctx.today
        min_units = _resolve(cond.min_units, ctx, params)
        start = anchor - timedelta(days=round(365.25 * (cond.years or 3)))
        hits = []
        for f in known:
            end = f.value.get("end")
            end = date.fromisoformat(end) if isinstance(end, str) else end
            units = f.value.get("units")
            hits.append((end is not None and start <= end <= anchor
                         and (min_units is None or (units or 0) >= min_units), f))
        ok_facts = [f for ok, f in hits if ok]
        what = f"experience within {cond.years} years of {anchor.isoformat()}" + (f", >= {min_units:g} items" if min_units else "")
        if not ok_facts:
            return _settle(False, known[0], ctx, params, what, proof)
        f = next((x for x in ok_facts if x.value.get("reference_letter") or x.evidence_status == "document_on_file"),
                 ok_facts[0])
        needs_ref = proof and not (f.value.get("reference_letter") or f.evidence_status == "document_on_file")
        if needs_ref:
            return RuleResult(NEEDS, f"{what}: {f.label} qualifies but no reference on file", basis="declared",
                              facts=[f.id], params=params, question="Can you provide a customer reference for this contract?",
                              needs_human="G2")
        return _settle(True, f, ctx, params, what, False)

    if cond.kind == "region_covered":
        regions = _resolve(cond.bound or "lot.regions", ctx, params) or []
        f = known[0]
        covered = [r for r in regions if any(r.startswith(p) or p.startswith(r) for p in f.value.get("itl", []))]
        if not regions:
            return RuleResult(UNKNOWN, "no delivery region in the notice", facts=[f.id], params=params)
        return _settle(len(covered) == len(regions), f, ctx, params,
                       f"delivery regions {', '.join(regions)} vs {', '.join(f.value.get('itl', []))}", False)

    if cond.kind == "min_amount":
        bound = _resolve(cond.bound, ctx, params)
        f = known[0]
        amt = _num(f, cond.fact)
        if amt is None or not isinstance(bound, (int, float)):
            return RuleResult(UNKNOWN, f"amount not available for {f.label}", facts=[f.id], params=params)
        return _settle(amt >= bound, f, ctx, params, f"{f.label} {amt:,.0f} >= {bound:,.0f}", proof)

    f = next((x for x in known if x.evidence_status == "document_on_file"), known[0])
    return _settle(True, f, ctx, params, f"{f.label} held", proof)
