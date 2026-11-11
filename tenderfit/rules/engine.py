import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel

from tenderfit.profile.validity import FactView, check

MET, NOT_MET, NEEDS, UNKNOWN = "met", "not_met", "needs_confirmation", "unknown"
PROOF_CATEGORIES = {"certificate", "insurance", "standard", "experience"}
WORKLOAD_PATHS = {"capacity.lead_days", "workload", "staff", "installation_slots"}
SCOPED_ATTRIBUTES = {"capacity", "insurance"}  # a value here only means something for a stated product or policy type

INSURANCE_TYPES = {
    "public liability": ("public liability", "public_liability", "third party liability"),
    "employers liability": ("employers liability", "employer's liability", "employers' liability", "employer liability",
                            "employers_liability"),
    "professional indemnity": ("professional indemnity", "professional_indemnity"),
    "product liability": ("product liability", "products liability", "product_liability"),
}
TERM_ALIASES = {"chair": "seat", "seating": "seat", "stool": "seat", "desking": "desk"}
GENERIC_TERMS = {"item", "unit", "product", "good", "supply", "all", "the", "and", "for", "with", "of", "to", "a", "an", "new"}
DAYS_PER_UNIT = {None: 1, "day": 1, "days": 1, "calendar day": 1, "calendar days": 1, "week": 7, "weeks": 7}


class Outcome(StrEnum):
    SETTLED = "settled"          # the rule reached a defensible answer from structured facts
    NO_EVIDENCE = "no_evidence"  # the profile holds nothing that could answer it; a person or the agent must add it
    NEEDS_MODEL = "needs_model"  # the condition cannot be judged by rule; the text must be interpreted


class Condition(BaseModel):
    kind: Literal["at_least", "at_most", "within_years", "region_covered", "has_evidence", "min_amount", "valid_through"]
    fact: str
    bound: float | str | None = None
    years: int | None = None
    anchor: str | None = None
    min_units: float | str | None = None
    match: str | None = None
    qualifier: str | None = None  # product, scope or policy type the value applies to
    unit: str | None = None       # unit of the bound as stated in the tender


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
    declaration_confirmed: bool = False

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
    outcome: Outcome = Outcome.SETTLED
    required: str | None = None  # what the tender asks for, in one phrase
    observed: str | None = None  # what the supplier fact says, in one phrase


def _resolve(v, ctx: Context, params: list[str]):
    if isinstance(v, str) and v.split(".")[0] in ("notice", "lot"):
        params.append(v)
        return ctx.fields.get(v)
    return v


def _number(v) -> float | None:
    """A finite, non-negative number or nothing: text, booleans, NaN and infinity never reach a comparison."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) and v >= 0 else None


def _date(v) -> date | None:
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _num(fact: Fact, path: str) -> float | None:
    key = path.split(".", 1)[1] if "." in path else None
    return _number(fact.value.get(key)) if key else None


def _terms(text: str | None) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", (text or "").lower().replace("_", " ")):
        w = w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
        w = TERM_ALIASES.get(w, w)
        if w not in GENERIC_TERMS and len(w) > 2:
            out.add(w)
    return out


def insurance_type_in(text: str | None) -> str | None:
    """The single policy type a text names, or None when it names none or several."""
    low = (text or "").lower().replace("_", " ")
    found = {name for name, words in INSURANCE_TYPES.items() if any(w.replace("_", " ") in low for w in words)}
    return found.pop() if len(found) == 1 else None


def _fact_scope(f: Fact) -> str | None:
    if f.attribute == "insurance":
        return insurance_type_in(str(f.value.get("type") or f.label))
    if f.attribute == "test_report":
        return f.value.get("product")
    return f.value.get("scope") or f.value.get("category")


def _normal(text: object) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip() + " "


def _matches_evidence(match: str, f: Fact) -> bool:
    """Whole-token match, so 'ISO 900' never matches 'ISO 9001', while 'ISO 9001' does match 'ISO 9001:2015'."""
    needle = _normal(match)
    return needle in _normal(f.value) or needle in _normal(f.label)


@dataclass
class Selection:
    compatible: list[Fact] = field(default_factory=list)
    ambiguous: list[Fact] = field(default_factory=list)
    excluded: list[Fact] = field(default_factory=list)


def _select(cond: Condition, facts: list[Fact], requirement: str | None) -> Selection:
    """Sort facts by whether they can speak to this condition. Listing order never decides the result."""
    attr = cond.fact.split(".")[0]
    wanted = cond.qualifier
    if attr == "insurance":
        wanted = insurance_type_in(wanted) or insurance_type_in(requirement) or wanted
    out = Selection()
    for f in sorted((f for f in facts if f.attribute == attr), key=lambda f: f.id):
        have = _fact_scope(f)
        if attr == "insurance":
            if not wanted or not have:
                (out.compatible if not have and not wanted else out.ambiguous).append(f)
            else:
                (out.compatible if have == wanted else out.excluded).append(f)
        elif wanted:
            text = have if have else (f"{f.label} {f.value.get('description', '')}" if attr == "experience" else None)
            if not text:
                out.ambiguous.append(f)
            else:
                (out.compatible if _terms(text) & _terms(wanted) else out.excluded).append(f)
        elif attr in SCOPED_ATTRIBUTES and have:
            if not requirement:
                out.ambiguous.append(f)
            else:
                (out.compatible if _terms(have) & _terms(requirement) else out.excluded).append(f)
        else:
            out.compatible.append(f)
    return out


def _rank(f: Fact) -> tuple:
    return (0 if f.evidence_status == "document_on_file" else 1, f.id)


def _needs_model(reason: str, params: list[str], facts: list[Fact] | None = None) -> RuleResult:
    return RuleResult(UNKNOWN, reason, facts=[f.id for f in facts or []], params=params, outcome=Outcome.NEEDS_MODEL)


def _settle(ok: bool, fact: Fact, ctx: Context, params: list[str], what: str, proof_needed: bool,
            workload: bool = False, required: str | None = None) -> RuleResult:
    v = check(fact.view(), today=ctx.today, deadline=ctx.deadline)
    base: dict[str, Any] = {"facts": [fact.id], "params": params, "required": required, "observed": fact.label}
    if not ok:
        if v.usable and not v.stale:
            return RuleResult(NOT_MET, f"{what}: contrary evidence ({fact.label})", needs_human="G6", **base)
        return RuleResult(NEEDS, f"{what}: the fact that contradicts it is out of date ({v.reason})",
                          question=f"Please update: {fact.label}", needs_human="G4", **base)
    if not v.usable:
        return RuleResult(NEEDS, f"{what}: {v.reason}", question=v.question or f"Please update: {fact.label}",
                          needs_human="G4", **base)
    if workload and not fact.confirmed_for_lot:
        return RuleResult(NEEDS, f"{what} (declared); current workload not confirmed for this lot", basis="declared",
                          question=f"Can you confirm '{fact.label}' for this order?", needs_human="G3", **base)
    if v.stale:
        return RuleResult(NEEDS, f"{what}: {v.reason}", question=v.question, needs_human="G4", **base)
    if proof_needed and fact.evidence_status == "declared" and fact.declaration_confirmed:
        return RuleResult(MET, f"{what} (on declared facts, declaration confirmed for this lot)", basis="declared", **base)
    if proof_needed and fact.evidence_status == "declared":
        return RuleResult(NEEDS, f"{what} on declared facts; proof is required", basis="declared",
                          question=f"Upload evidence for '{fact.label}' or confirm the declaration.", needs_human="G2", **base)
    basis = "documents" if fact.evidence_status == "document_on_file" else "declared"
    return RuleResult(MET, f"{what} ({'on documents' if basis == 'documents' else 'on declared facts'})", basis=basis, **base)


def _conflict(what: str, supporting: list[Fact], contrary: list[Fact], params: list[str], required: str) -> RuleResult:
    names = ", ".join(f.label for f in supporting + contrary)
    return RuleResult(NEEDS, f"{what}: the profile holds conflicting facts ({names})", basis="declared",
                      facts=sorted(f.id for f in supporting + contrary), params=params, needs_human="G4",
                      question=f"Which of these is current: {names}?", required=required, observed=names)


def _compare(cond: Condition, sel: Selection, ctx: Context, params: list[str], proof: bool) -> RuleResult:
    """at_least, at_most and min_amount share one path: every compatible fact is compared, then the results combined."""
    bound = _number(_resolve(cond.bound, ctx, params))
    if bound is None:
        return _needs_model(f"bound {cond.bound} not available", params, sel.compatible)
    if cond.fact.endswith("lead_days"):
        factor = DAYS_PER_UNIT.get((cond.unit or "").lower() or None)
        if factor is None:
            return RuleResult(NEEDS, f"the tender states this in {cond.unit}, which cannot be compared exactly with a lead time in days",
                              facts=[f.id for f in sel.compatible], params=params, needs_human="G5",
                              question=f"How many calendar days do {cond.bound:g} {cond.unit} mean in this tender?")
        bound *= factor
    valued = [(f, v) for f in sel.compatible if (v := _num(f, cond.fact)) is not None]
    if not valued:
        return _needs_model(f"profile has no value for {cond.fact}", params, sel.compatible + sel.ambiguous)
    money = cond.kind == "min_amount"
    at_least = cond.kind in ("at_least", "min_amount")
    supporting = sorted((f for f, v in valued if (v >= bound if at_least else v <= bound)), key=_rank)
    contrary = sorted((f for f, v in valued if not (v >= bound if at_least else v <= bound)), key=_rank)
    values = dict((f.id, v) for f, v in valued)
    name, unit = {"capacity.lead_days": ("lead time", "days"), "capacity.max_units": ("capacity", "units")}.get(
        cond.fact, (cond.fact.split(".")[-1].replace("_", " "), ""))
    required = f"£{bound:,.0f}" if money else f"{'at least' if at_least else 'at most'} {bound:,g} {unit}".strip()

    def what(f: Fact, ok: bool) -> str:
        v = values[f.id]
        if money:
            return f"{f.label} £{v:,.0f} {'meets' if ok else 'is below'} the required £{bound:,.0f}"
        if at_least:
            return f"{name} {v:,g} {unit} {'covers' if ok else 'falls short of'} the required {bound:,g}".replace("  ", " ")
        return f"{name} {v:g} {unit} {'is within' if ok else 'exceeds'} the required {bound:g}".replace("  ", " ")

    if supporting and contrary:
        return _conflict(what(contrary[0], False), supporting, contrary, params, required)
    f = (supporting or contrary)[0]
    ok = bool(supporting)
    workload = cond.fact in WORKLOAD_PATHS or cond.fact.split(".")[0] in WORKLOAD_PATHS
    return _settle(ok, f, ctx, params, what(f, ok), proof, workload=workload, required=required)


def _within_years(cond: Condition, sel: Selection, ctx: Context, params: list[str], proof: bool) -> RuleResult:
    anchor = _date(_resolve(cond.anchor or "notice.deadline", ctx, params)) or (ctx.today if not cond.anchor else None)
    if anchor is None or not isinstance(cond.years, int) or cond.years <= 0:
        return _needs_model("the time window of this condition could not be read", params, sel.compatible)
    raw_min = _resolve(cond.min_units, ctx, params)
    min_units = _number(raw_min)
    if raw_min is not None and min_units is None:
        return _needs_model(f"bound {cond.min_units} not available", params, sel.compatible)
    start = anchor - timedelta(days=round(365.25 * cond.years))
    qualifying, incomplete, outside = [], [], []
    for f in sel.compatible:
        end, units = _date(f.value.get("end")), _number(f.value.get("units"))
        if end is None or (min_units is not None and units is None):
            incomplete.append(f)
        elif start <= end <= anchor and (min_units is None or (units is not None and units >= min_units)):
            qualifying.append(f)
        else:
            outside.append(f)
    what = f"experience within {cond.years} years of {anchor.strftime('%-d %b %Y')}" + (f", at least {min_units:g} items" if min_units else "")
    if not qualifying:
        # A list of contracts is never exhaustive, so what it omits is not evidence against the supplier.
        gap = (f"{what}: {incomplete[0].label} has no end date or size on record" if incomplete else
               f"{what}: none of the contracts on record qualifies")
        ids = sorted(f.id for f in incomplete + outside)
        return RuleResult(NEEDS, gap, basis="declared", facts=ids, params=params, required=what,
                          question="Can you add the dates and size of a contract that meets this?")
    f = sorted(qualifying, key=lambda x: (not (x.value.get("reference_letter") or x.evidence_status == "document_on_file"), x.id))[0]
    if proof and not (f.value.get("reference_letter") or f.evidence_status == "document_on_file" or f.declaration_confirmed):
        return RuleResult(NEEDS, f"{what}: {f.label} qualifies but no reference on file", basis="declared", facts=[f.id],
                          params=params, question="Can you provide a customer reference for this contract?",
                          needs_human="G2", required=what, observed=f.label)
    return _settle(True, f, ctx, params, what, False, required=what)


def _has_evidence(cond: Condition, sel: Selection, ctx: Context, params: list[str], proof: bool, equivalent_allowed: bool) -> RuleResult:
    if not cond.match or not cond.match.strip():
        return _needs_model("the condition does not say which evidence is required", params, sel.compatible)
    named = [f for f in sel.compatible + sel.ambiguous if _matches_evidence(cond.match, f)]
    held = sorted((f for f in named if f.evidence_status not in ("missing", "unknown")), key=_rank)
    if not held:
        if named:
            f = named[0]
            return RuleResult(UNKNOWN, f"{f.label}: evidence {f.evidence_status}, not proven either way", facts=[f.id],
                              params=params, question=f"Do you hold {f.label}?", outcome=Outcome.NO_EVIDENCE, required=cond.match)
        others = [f for f in sel.compatible if f.evidence_status not in ("missing", "unknown")]
        if equivalent_allowed and others:
            names = ", ".join(f.label for f in others)
            return RuleResult(NEEDS, f"the tender accepts an equivalent to {cond.match}; you hold {names}, which the buyer would have to accept",
                              facts=[f.id for f in others], params=params, needs_human="G5", required=f"{cond.match} or equivalent",
                              observed=names, question=f"Does the buyer accept {names} as equivalent to {cond.match}?")
        return RuleResult(UNKNOWN, f"no {cond.fact.split('.')[0]} fact in the profile covers {cond.match}", params=params,
                          question=f"Do you have {cond.match}?", outcome=Outcome.NO_EVIDENCE, required=cond.match)
    best = next((f for f in held if check(f.view(), today=ctx.today, deadline=ctx.deadline).usable), held[0])
    if best in sel.ambiguous:
        return RuleResult(NEEDS, f"{best.label} may not cover {cond.qualifier}", facts=[best.id], params=params, needs_human="G2",
                          question=f"Does {best.label} cover {cond.qualifier}?", required=cond.match, observed=best.label)
    return _settle(True, best, ctx, params, f"{best.label} held", proof, required=cond.match)


def _valid_through(cond: Condition, sel: Selection, ctx: Context, params: list[str], proof: bool) -> RuleResult:
    anchor = _date(_resolve(cond.anchor, ctx, params))
    if anchor is None:
        return _needs_model("the date this must stay valid until is not available", params, sel.compatible)
    named = [f for f in sel.compatible if (_matches_evidence(cond.match, f) if cond.match else True)
             and f.evidence_status not in ("missing", "unknown")]
    if not named or (not cond.match and len(named) > 1):
        return RuleResult(UNKNOWN, f"no fact shows {cond.match or cond.fact} valid until {anchor}", params=params,
                          facts=[f.id for f in named], outcome=Outcome.NO_EVIDENCE, required=f"valid until {anchor}",
                          question=f"Please add {cond.match or cond.fact} with its expiry date.")
    f = sorted(named, key=lambda x: (-(x.valid_until or date.min).toordinal(), x.id))[0]
    required = f"valid until {anchor.strftime('%-d %b %Y')}"
    if f.valid_until is None:
        return RuleResult(NEEDS, f"{f.label} has no expiry date on record, so it cannot be shown {required}", facts=[f.id], params=params,
                          needs_human="G4", question=f"When does {f.label} expire?", required=required, observed=f.label)
    if f.valid_until < anchor:
        return RuleResult(NEEDS, f"{f.label} ends {f.valid_until.isoformat()}, before {anchor.isoformat()}; a renewal would satisfy this",
                          facts=[f.id], params=params, needs_human="G4", required=required, observed=f.label,
                          question=f"Will {f.label} be renewed before {anchor.isoformat()}?")
    return _settle(True, f, ctx, params, f"{f.label} valid until {f.valid_until.isoformat()}", proof, required=required)


def _evaluate(cond: Condition, facts: list[Fact], ctx: Context, category: str, proof_needed: bool, requirement: str | None,
              equivalent_allowed: bool) -> RuleResult:
    params: list[str] = []
    proof = proof_needed or category in PROOF_CATEGORIES
    attr = cond.fact.split(".")[0]
    sel = _select(cond, facts, requirement)
    if not sel.compatible and not sel.ambiguous:
        if sel.excluded:
            names = ", ".join(f.label for f in sel.excluded)
            what = cond.qualifier or insurance_type_in(requirement) or "this requirement"
            return RuleResult(UNKNOWN, f"no {attr} fact covers {what}; the profile only holds {names}", params=params,
                              question=f"Do you have {attr} that covers {what}?", outcome=Outcome.NO_EVIDENCE, required=str(what))
        return RuleResult(UNKNOWN, f"no {attr} fact in the profile", params=params,
                          question=f"Do you have {cond.match or attr}?", outcome=Outcome.NO_EVIDENCE, required=cond.match)
    known = Selection([f for f in sel.compatible if f.evidence_status not in ("missing", "unknown")],
                      [f for f in sel.ambiguous if f.evidence_status not in ("missing", "unknown")], sel.excluded)
    if cond.kind != "has_evidence" and not known.compatible and not known.ambiguous:
        f = (sel.compatible + sel.ambiguous)[0]
        return RuleResult(UNKNOWN, f"{f.label}: evidence {f.evidence_status}, not proven either way", facts=[f.id],
                          params=params, question=f"Do you hold {f.label}?", outcome=Outcome.NO_EVIDENCE)
    if cond.kind in ("at_least", "at_most", "min_amount"):
        if not known.compatible:
            return _needs_model(f"the requirement does not say which {attr} it means", params, known.ambiguous)
        return _compare(cond, known, ctx, params, proof)
    if cond.kind == "within_years":
        return _within_years(cond, known, ctx, params, proof)
    if cond.kind == "region_covered":
        regions = _resolve(cond.bound or "lot.regions", ctx, params) or []
        if not isinstance(regions, (list, tuple)) or not regions:
            return _needs_model("no delivery region in the notice", params, known.compatible)
        pool = sorted(known.compatible, key=_rank)
        itl = [p for f in pool for p in f.value.get("itl", [])]
        if not itl:
            return _needs_model("the profile lists no delivery regions", params, pool)
        all_covered = all(any(r.startswith(p) or p.startswith(r) for p in itl) for r in regions)
        return _settle(all_covered, pool[0], ctx, params,
                       f"delivery regions {', '.join(regions)} {'covered' if all_covered else 'not all covered'} by {pool[0].label}", False,
                       required=f"delivery in {', '.join(regions)}")
    if cond.kind == "valid_through":
        return _valid_through(cond, known, ctx, params, proof)
    return _has_evidence(cond, sel, ctx, params, proof, equivalent_allowed)


def evaluate(cond: Condition, facts: list[Fact], ctx: Context, *, category: str = "other", proof_needed: bool = False,
             requirement: str | None = None, equivalent_allowed: bool = False) -> RuleResult:
    """Judge one condition against supplier facts. `requirement` is the tender's wording, used to scope facts.

    Bad input never raises: a condition that cannot be evaluated is handed on as NEEDS_MODEL, never treated as proven."""
    try:
        return _evaluate(cond, facts, ctx, category, proof_needed, requirement, equivalent_allowed)
    except (TypeError, ValueError, OverflowError, AttributeError) as e:
        return _needs_model(f"this condition could not be evaluated ({type(e).__name__})", [])
