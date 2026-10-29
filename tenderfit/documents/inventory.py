import re
from dataclasses import dataclass

from tenderfit.documents.parse import Block

SUPPORTING_TYPES = {"conflictOfInterest", "billOfQuantity"}
SUPPORTING_TITLE = re.compile(r"pricing|price schedule|form of tender|declaration|conflict of interest|response template|"
                              r"bidder response|tender response|bill of quantities", re.I)
NAMED_TO_TYPE = {
    "specification": "technicalSpecifications",
    "instructions to tenderers": "biddingDocuments",
    "instructions to bidders": "biddingDocuments",
    "invitation to tender": "biddingDocuments",
    "evaluation criteria": "evaluationCriteria",
    "selection questionnaire": "eligibilityCriteria",
    "terms and conditions": "contractDraft",
    "conditions of contract": "contractDraft",
    "clarification log": "clarifications",
}


def is_essential(ocds_type: str | None, label: str | None) -> bool:
    if ocds_type in SUPPORTING_TYPES:
        return False
    if label and SUPPORTING_TITLE.search(label):
        return False
    return True


@dataclass
class RefState:
    kind: str
    essential: bool
    status: str


def coverage(refs: list[RefState]) -> str:
    docs = [r for r in refs if r.kind != "notice"]
    if not any(r.status == "read" for r in docs):
        return "notice_only"
    if any(r.essential and r.status not in ("read", "confirmed_none") for r in docs):
        return "partial"
    return "complete"


def _txt(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "", [], {}) else ""


def notice_blocks(release: dict) -> list[Block]:
    t = release.get("tender", {})
    out: list[Block] = []

    def add(heading: str, *parts):
        body = "\n".join(p for p in (_txt(x) for x in parts) if p)
        if body:
            out.append(Block(heading, None, heading=True))
            out.append(Block(body, None))

    add("Notice: description", t.get("title"), t.get("description"))
    period = t.get("tenderPeriod", {})
    add("Notice: submission", f"Submission deadline: {period.get('endDate')}" if period.get("endDate") else "",
        t.get("submissionMethodDetails"))
    for lot in t.get("lots", []):
        lid = lot.get("id")
        items = [i for i in t.get("items", []) if str(i.get("relatedLot", lid)) == str(lid)]
        qty = "; ".join(f"Quantity {i['quantity']} {i.get('unit', {}).get('name', '')}".strip() for i in items if i.get("quantity"))
        cp = lot.get("contractPeriod", {})
        period_txt = ", ".join(f"{k}: {v}" for k, v in cp.items()) if cp else ""
        add(f"Notice: lot {lid}", lot.get("title"), lot.get("description"), qty,
            f"Contract period {period_txt}" if period_txt else "")
        crit = (lot.get("awardCriteria") or {}).get("criteria", [])
        add(f"Notice: lot {lid} award criteria",
            *[f"{c.get('name') or c.get('type')}: {c.get('description') or ''}" for c in crit])
    sel = (t.get("selectionCriteria") or {}).get("criteria", [])
    add("Notice: conditions of participation",
        *[" ".join(_txt(c.get(k)) for k in ("type", "description", "minimum")) for c in sel])
    other = t.get("otherRequirements") or {}
    add("Notice: other requirements", *[f"{k}: {v}" for k, v in other.items()])
    return out
