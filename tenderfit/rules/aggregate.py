from dataclasses import dataclass, field

from tenderfit.rules.engine import MET, NEEDS, NOT_MET, UNKNOWN


@dataclass
class V:
    requirement_id: int
    statement: str
    obligation: str
    category: str
    status: str
    reason: str
    source: str = ""
    question: str | None = None
    basis: str | None = None


@dataclass
class Ref:
    label: str
    kind: str
    essential: bool
    status: str


def eligibility(verdicts: list[V], refs: list[Ref]) -> tuple[str, list]:
    blockers = [v for v in verdicts if v.obligation == "mandatory" and v.status == NOT_MET]
    if blockers:
        return "blocker_found", blockers
    if not any(r.status == "read" for r in refs if r.kind != "notice"):
        return "not_assessed", []
    unread = [r for r in refs if r.essential and r.kind != "notice" and r.status not in ("read", "confirmed_none")]
    if unread:
        return "provisional", unread
    open_items = [v for v in verdicts if v.obligation == "mandatory" and v.status != MET]
    return ("needs_confirmation", open_items) if open_items else ("no_blocker", [])


@dataclass
class Dimensions:
    relevance: dict
    eligibility: dict
    evidence: dict
    feasibility: dict
    open_questions: dict
    recommendation: str
    provisional: bool
    coverage: str
    notes: list[str] = field(default_factory=list)


def relevance(lot_cpv: list[str], product_cpv: list[str], lot_regions: list[str], profile_regions: list[str]) -> dict:
    def overlap(n):
        return any(a[:n] == b[:n] for a in lot_cpv for b in product_cpv)
    regions_ok = not lot_regions or all(any(r.startswith(p) or p.startswith(r) for p in profile_regions) for r in lot_regions)
    if overlap(4) and regions_ok:
        return {"level": "strong", "detail": "products and region covered"}
    if overlap(3):
        return {"level": "partial", "detail": "related products" + ("" if regions_ok else "; region not covered")}
    if overlap(4):
        return {"level": "partial", "detail": "products match; region not covered"}
    return {"level": "low", "detail": "products not in your profile"}


def dimensions(verdicts: list[V], refs: list[Ref], rel: dict, coverage: str) -> Dimensions:
    elig, items = eligibility(verdicts, refs)
    docs = [r for r in refs if r.kind != "notice" and r.essential]
    read = sum(r.status in ("read", "confirmed_none") for r in docs)
    mand = [v for v in verdicts if v.obligation == "mandatory"]
    met = sum(v.status == MET for v in mand)
    if elig == "blocker_found":
        label, detail = "Blocker found", "; ".join(f"{b.statement} ({b.source})" for b in items)
    elif elig == "not_assessed":
        label, detail = "Not assessed", "tender documents unavailable"
    elif elig == "provisional":
        label, detail = "Provisional", f"no blocker in documents read ({read} of {len(docs)}); unread: " + \
            ", ".join(r.label for r in items)
    else:
        label = "No blocker found"
        detail = f"{met} of {len(mand)} met" + (f" · {len(items)} to confirm" if items else "")
    gaps = [v for v in verdicts if v.status in (NEEDS, UNKNOWN) and v.category in
            ("certificate", "standard", "experience", "insurance", "financial")]
    feas = [v for v in verdicts if v.category in ("quantity", "delivery")]
    if any(v.status == NOT_MET for v in feas):
        feasibility = {"level": "blocker", "detail": next(v.reason for v in feas if v.status == NOT_MET)}
    elif any(v.status == NEEDS for v in feas):
        feasibility = {"level": "confirm", "detail": next(v.reason for v in feas if v.status == NEEDS)}
    elif feas and all(v.status == MET for v in feas):
        feasibility = {"level": "feasible", "detail": "quantities and delivery fit"}
    else:
        feasibility = {"level": "unknown", "detail": "no capacity or delivery checks settled"}
    questions = [v.question for v in verdicts if v.question]

    if elig == "blocker_found":
        rec = f"Blocker found: {items[0].statement} ({items[0].source})"
    elif elig == "not_assessed":
        rec = "Not assessed beyond relevance: tender documents unavailable"
    elif rel["level"] == "low":
        rec = f"Low relevance: {rel['detail']}"
    elif elig == "provisional":
        rec = f"Provisional: no blocker found in documents read ({read} of {len(docs)})"
    elif elig == "needs_confirmation":
        rec = f"Relevant: {len(items)} item{'s' if len(items) != 1 else ''} need confirmation before you bid"
    else:
        rec = "Relevant: no mandatory blocker found"
    return Dimensions(
        relevance=rel,
        eligibility={"state": elig, "label": label, "detail": detail},
        evidence={"gaps": len(gaps), "detail": ", ".join(v.statement for v in gaps[:3]) or "no gaps"},
        feasibility=feasibility,
        open_questions={"count": len(questions), "items": questions},
        recommendation=rec,
        provisional=elig in ("provisional", "not_assessed"),
        coverage=coverage,
    )
