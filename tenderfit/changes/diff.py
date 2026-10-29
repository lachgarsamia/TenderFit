from dataclasses import dataclass, field

from rapidfuzz import fuzz

MODIFIED_AT, AMBIGUOUS_BELOW = 90.0, 85.0
STRUCTURAL_SECTIONS = ("conditions of participation", "award criteria", "defin", "evaluation", "selection")


@dataclass
class FieldChange:
    path: str
    before: object
    after: object


@dataclass
class PassageChange:
    kind: str  # unchanged | modified | added | removed
    old_id: int | None
    new_id: int | None
    section: str | None
    confidence: float
    document: str


@dataclass
class Change:
    fields: list[FieldChange] = field(default_factory=list)
    passages: list[PassageChange] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)  # {"kind": added|removed|replaced|unreadable, "label", "essential"}
    cls: str = "local"
    reasons: list[str] = field(default_factory=list)

    @property
    def changed_passages(self) -> set[int]:
        return {p.old_id for p in self.passages if p.kind in ("modified", "removed") and p.old_id}

    @property
    def new_passage_ids(self) -> set[int]:
        return {p.new_id for p in self.passages if p.kind in ("modified", "added") and p.new_id}

    @property
    def passage_map(self) -> dict[int, int]:
        return {p.old_id: p.new_id for p in self.passages if p.old_id and p.new_id}


def _lot_key(lot: dict) -> str:
    return str(lot.get("id"))


def field_diff(old: dict, new: dict) -> list[FieldChange]:
    out = []
    for key, path in [("deadline", "notice.deadline"), ("title", "notice.title"), ("value", "notice.value"),
                      ("cpv", "notice.cpv"), ("regions", "lot.regions"), ("legal_basis", "notice.legal_basis")]:
        if old.get(key) != new.get(key):
            out.append(FieldChange(path, old.get(key), new.get(key)))
    ol, nl = {_lot_key(l): l for l in old.get("lots", [])}, {_lot_key(l): l for l in new.get("lots", [])}
    for lid in sorted(set(ol) | set(nl)):
        if lid not in nl:
            out.append(FieldChange(f"lots.{lid}", "present", "removed"))
        elif lid not in ol:
            out.append(FieldChange(f"lots.{lid}", "absent", "added"))
        else:
            for k, path in [("quantity", "lot.quantity"), ("regions", "lot.regions"), ("cpv", "lot.cpv"),
                            ("title", "lot.title"), ("description", "lot.description"), ("value", "lot.value")]:
                if ol[lid].get(k) != nl[lid].get(k):
                    out.append(FieldChange(f"{path}@{lid}", ol[lid].get(k), nl[lid].get(k)))
    od = {(d.get("documentType"), d.get("title"), d.get("url")) for d in old.get("documents", []) if not d.get("noticeType")}
    nd = {(d.get("documentType"), d.get("title"), d.get("url")) for d in new.get("documents", []) if not d.get("noticeType")}
    if od != nd:
        out.append(FieldChange("notice.documents", sorted(map(str, od - nd)), sorted(map(str, nd - od))))
    return out


def align(old: list[dict], new: list[dict], document: str) -> list[PassageChange]:
    """Passages are {id, sha256, section, text}. Hash first, then fuzzy within the same section."""
    out: list[PassageChange] = []
    by_hash: dict[str, list[dict]] = {}
    for p in new:
        by_hash.setdefault(p["sha256"], []).append(p)
    used: set[int] = set()
    rest_old = []
    for p in old:
        cands = [n for n in by_hash.get(p["sha256"], []) if n["id"] not in used]
        if cands:
            used.add(cands[0]["id"])
            out.append(PassageChange("unchanged", p["id"], cands[0]["id"], p["section"], 100.0, document))
        else:
            rest_old.append(p)
    rest_new = [n for n in new if n["id"] not in used]
    for p in rest_old:
        same = [n for n in rest_new if n["id"] not in used and (n["section"] or "") == (p["section"] or "")]
        pool = same or [n for n in rest_new if n["id"] not in used]
        best, score = None, 0.0
        for n in pool:
            s = fuzz.token_set_ratio(p["text"], n["text"]) * (1.0 if n in same else 0.97)
            if s > score:
                best, score = n, s
        if best is not None and score >= AMBIGUOUS_BELOW:
            used.add(best["id"])
            out.append(PassageChange("modified", p["id"], best["id"], p["section"], score, document))
        else:
            out.append(PassageChange("removed", p["id"], None, p["section"], score, document))
    for n in rest_new:
        if n["id"] not in used:
            out.append(PassageChange("added", None, n["id"], n["section"], 0.0, document))
    return out


def classify(change: Change, mandatory_passages: set[int], lot_count_changed: bool = False) -> Change:
    reasons = []
    if lot_count_changed or any(f.path.startswith("lots.") for f in change.fields):
        reasons.append(("structural", "lots added, removed or split"))
    for d in change.documents:
        if d["kind"] in ("added", "removed", "replaced") and d.get("essential"):
            reasons.append(("structural", f"essential document {d['kind']}: {d['label']}"))
        if d["kind"] == "unreadable":
            reasons.append(("ambiguous", f"new document could not be read: {d['label']}"))
    for p in change.passages:
        if p.kind != "unchanged" and p.section and any(s in p.section.lower() for s in STRUCTURAL_SECTIONS):
            reasons.append(("structural", f"{p.section} changed"))
        if p.old_id in mandatory_passages and (p.kind == "removed" or (p.kind == "modified" and p.confidence < MODIFIED_AT)):
            reasons.append(("ambiguous", f"low alignment confidence ({p.confidence:.0f}) for a passage cited by a mandatory requirement"))
    by_doc: dict[str, list[PassageChange]] = {}
    for p in change.passages:
        by_doc.setdefault(p.document, []).append(p)
    for doc, ps in by_doc.items():
        olds = [p for p in ps if p.old_id]
        if olds and sum(p.kind != "unchanged" for p in olds) / len(olds) > 0.30 and len(olds) >= 4:
            reasons.append(("structural", f"more than 30% of {doc} changed"))
    if not change.fields and not any(p.kind != "unchanged" for p in change.passages) and not change.documents:
        reasons.append(("ambiguous", "new version with no detectable difference"))
    rank = {"local": 0, "structural": 1, "ambiguous": 2}
    change.cls = max((c for c, _ in reasons), key=rank.get, default="local")
    change.reasons = list(dict.fromkeys(r for _, r in reasons))
    return change
