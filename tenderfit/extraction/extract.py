import re

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from tenderfit.documents import references
from tenderfit.documents.text import QUOTE_THRESHOLD, normalise, quote_score, words
from tenderfit.extraction.prompts import EXTRACT_SYSTEM, EXTRACT_VERSION, extract_user
from tenderfit.extraction.schema import EXTRACTION_SCHEMA, Requirement
from tenderfit.extraction.validate import check_condition, normalise_lot, numbers_in, same_digits
from tenderfit.llm import LLM, LLMError, OutputTooLong, log_call

BATCH_WORDS = 3000


def _batches(passages: list[dict]) -> list[list[dict]]:
    out, cur, n = [], [], 0
    for p in passages:
        w = words(p["text"])
        if cur and n + w > BATCH_WORDS:
            out.append(cur)
            cur, n = [], 0
        cur.append(p)
        n += w
    if cur:
        out.append(cur)
    return out


def verify(req: Requirement, by_id: dict[int, dict]) -> tuple[int | None, float]:
    """Return the passage the quote really comes from, checking the cited one first, then its neighbours."""
    cited = by_id.get(req.passage_id)
    if cited is None:
        return None, 0.0
    best = (cited["id"], quote_score(req.quote, cited["text"]))
    if best[1] >= QUOTE_THRESHOLD and same_digits(req.quote, cited["text"]):
        return best
    for p in by_id.values():
        if p["document_id"] == cited["document_id"] and abs(p["ordinal"] - cited["ordinal"]) <= 1 and p["id"] != cited["id"]:
            joined = cited["text"] + " " + p["text"] if p["ordinal"] > cited["ordinal"] else p["text"] + " " + cited["text"]
            s = quote_score(req.quote, joined)
            if s > best[1] and same_digits(req.quote, joined):
                best = (cited["id"], s)
    return best if best[1] >= QUOTE_THRESHOLD and same_digits(req.quote, _source_text(cited, by_id)) else (None, best[1])


def _source_text(cited: dict, by_id: dict[int, dict]) -> str:
    neighbours = [p["text"] for p in by_id.values() if p["document_id"] == cited["document_id"] and abs(p["ordinal"] - cited["ordinal"]) <= 1]
    return " ".join(neighbours) or cited["text"]


def _reference_targets(label: str, passage: dict, sections: dict[int, dict[str, list[int]]]) -> list[int]:
    """Every passage of the referenced section, in the citing document first, then in the rest of the pack."""
    low = label.lower().strip()
    key = low.removeprefix("section ").strip()

    def hits(by_section: dict[str, list[int]]) -> list[int]:
        out: list[int] = []
        for sec, ids in by_section.items():
            if sec == low or sec == key or sec.startswith((f"{low} ", f"{low}:", f"{low}.", f"{key} ", f"{key}.", f"{key}:")):
                out += ids
        return out

    own = hits(sections.get(passage["document_id"], {}))
    if own:
        return own
    return [pid for doc, by_section in sections.items() if doc != passage["document_id"] for pid in hits(by_section)]


def _record_dependencies(conn: psycopg.Connection, req_id: int, version_id: int, req: Requirement, passage: dict, sections: dict,
                         refs_by_label: dict[str, int], definitions: dict[str, int], conditions: list[dict]) -> None:
    deps = [("source", "passage", passage["id"])]
    # References in the quote or named by the extractor; a reference elsewhere in a long passage is not this requirement's.
    labels = set(req.references) | {r.label for r in references.find(req.quote)}
    for label in labels:
        targets = _reference_targets(label, passage, sections)
        if targets:
            deps += [("reference", "passage", t) for t in targets]
        elif label.lower() in refs_by_label:
            deps.append(("reference", "document", refs_by_label[label.lower()]))
    for term in req.defined_terms:
        pid = definitions.get(term.lower())
        deps.append(("definition", "passage", pid) if pid else ("definition", "field", f"term:{term.lower()}"))
    for c in conditions:
        for v in (c.get("bound"), c.get("anchor"), c.get("min_units")):
            if isinstance(v, str) and v.split(".")[0] in ("notice", "lot"):
                deps.append(("parameter", "field", v))
    for kind, ttype, tid in dict.fromkeys(deps):
        conn.execute(
            """INSERT INTO dependency (requirement_id, version_id, kind, target_type, target_id, recorded_by)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (req_id, version_id, kind, ttype, str(tid), "rule_engine" if kind == "parameter" else "extractor"))


def _reject(conn: psycopg.Connection, version_id: int, raw: dict, kind: str, reason: str, score: float | None = None,
            passage_id: int | None = None, lot_no: str | None = None) -> None:
    conn.execute(
        """INSERT INTO requirement_rejection (version_id, passage_id, statement, quote, score, reason, kind, obligation, lot_no)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (version_id, passage_id, raw.get("statement"), raw.get("quote"), score, reason, kind, raw.get("obligation"), lot_no))


def _store_conditions(conn: psycopg.Connection, req_id: int, req: Requirement, primary: dict | None, primary_note: str | None,
                      passage_text: str) -> list[dict]:
    """Persist the atomic conditions. Each is validated against its own quote; a bad one is kept without its machine check."""
    items = req.conditions or []
    if not items:
        rows = [(req.statement, primary, req.quote, True, req.evidence_needed, primary_note)]
    else:
        rows = []
        for item in items:
            quote = item.quote or req.quote
            if quote_score(quote, passage_text) < QUOTE_THRESHOLD or not same_digits(quote, passage_text):
                rows.append((item.label, None, None, item.mandatory, item.evidence_needed, "its quote was not found in the source"))
                continue
            checked = check_condition(item.condition, quote)
            rows.append((item.label, checked.condition, quote, item.mandatory, item.evidence_needed, checked.note))
    for i, (label, cond, quote, mandatory, evidence, note) in enumerate(rows):
        conn.execute(
            """INSERT INTO requirement_condition (requirement_id, ordinal, label, condition, quote, mandatory, evidence_needed, condition_note)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (req_id, i, label, Jsonb(cond) if cond else None, quote, mandatory, evidence, note))
    return [r[1] for r in rows if r[1]]


def _passages(conn: psycopg.Connection, version_id: int, profile_id: int | None = None) -> list[dict]:
    return conn.execute(
        """SELECT p.id, p.document_id, p.ordinal, p.page, p.section, p.text, coalesce(d.filename, 'document') AS document,
                  d.owner_profile_id AS owner
           FROM passage p JOIN document d ON d.id = p.document_id
           WHERE d.version_id = %s AND d.access = 'read' AND (d.owner_profile_id IS NULL OR d.owner_profile_id = %s)
           ORDER BY d.id, p.ordinal""", (version_id, profile_id)).fetchall()


def extract_version(conn: psycopg.Connection, version_id: int, llm: LLM, force: bool = False,
                    only: set[int] | None = None, commit: bool = True, profile_id: int | None = None) -> dict:
    """Extracts what is new in the public pack, and in this supplier's own uploads when a profile is given."""
    passages = _passages(conn, version_id, profile_id)
    by_id = {p["id"]: p for p in passages}
    if only is not None:
        targets = [p for p in passages if p["id"] in only]
    elif force:
        targets = passages
    else:
        pending = {r["id"] for r in conn.execute(
            """SELECT id FROM document WHERE version_id = %s AND access = 'read' AND extracted_at IS NULL
               AND (owner_profile_id IS NULL OR owner_profile_id = %s)""", (version_id, profile_id))}
        targets = [p for p in passages if p["document_id"] in pending]
        if not targets:
            return {"cached": True}
    lots = {r["lot_no"]: r["id"] for r in conn.execute(
        """SELECT l.lot_no, l.id FROM lot l JOIN notice_version nv ON nv.opportunity_id = l.opportunity_id
           WHERE nv.id = %s""", (version_id,))}
    sections: dict[int, dict[str, list[int]]] = {}
    for p in passages:
        if p["section"]:
            sections.setdefault(p["document_id"], {}).setdefault(p["section"].lower(), []).append(p["id"])
    definitions = {}
    for p in passages:
        if p["section"] and "defin" in p["section"].lower():
            for term in set(t for t in re.findall(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)*)\b", p["text"])):
                definitions.setdefault(term.lower(), p["id"])
    refs_by_label = {r["label"].lower(): r["id"] for r in conn.execute(
        "SELECT id, label FROM document_ref WHERE version_id = %s AND (owner_profile_id IS NULL OR owner_profile_id = %s)",
        (version_id, profile_id))}

    stats = {"accepted": 0, "rejected": 0, "duplicates": 0, "calls": 0, "cost_usd": 0.0, "conditions_dropped": 0}
    seen: set[tuple] = set()
    stats["ids"] = []
    lot_nos = sorted(lots)

    def run_batch(batch: list[dict]) -> None:
        try:
            res = llm.json("extract", EXTRACT_SYSTEM, extract_user(batch, lot_nos), EXTRACTION_SCHEMA)
        except OutputTooLong:
            if len(batch) == 1:
                raise
            mid = len(batch) // 2  # too many requirements for one response: split rather than lose the document
            run_batch(batch[:mid])
            run_batch(batch[mid:])
            return
        except LLMError:
            conn.rollback()
            raise
        stats["calls"] += 1
        stats["cost_usd"] += res.usage.cost_usd
        log_call(conn, "extract", EXTRACT_VERSION, res.usage, {"version_id": version_id, "passages": [p["id"] for p in batch]})
        items = res.data.get("requirements") if isinstance(res.data, dict) else None
        if not isinstance(items, list):
            _reject(conn, version_id, {}, "response", "model response had no requirement list")
            stats["rejected"] += 1
            return
        for raw in items:
            accept(raw if isinstance(raw, dict) else {})

    def accept(raw: dict) -> None:
        try:
            req = Requirement(**raw)
        except ValidationError as e:
            _reject(conn, version_id, raw, "invalid", f"invalid: {e.errors()[0]['msg']}")
            stats["rejected"] += 1
            return
        except TypeError as e:
            _reject(conn, version_id, raw, "invalid", f"invalid: {e}")
            stats["rejected"] += 1
            return
        pid, score = verify(req, by_id)
        known_passage = req.passage_id if req.passage_id in by_id else None
        if pid is None:
            _reject(conn, version_id, raw, "quote", "quote not found in source passage", score, known_passage)
            stats["rejected"] += 1
            return
        p = by_id[pid]
        if not numbers_in(req.statement) <= numbers_in(_source_text(p, by_id)):
            _reject(conn, version_id, raw, "statement", "the statement gives a number that is not in the source", score, pid)
            stats["rejected"] += 1
            return
        lot_no, recognised = normalise_lot(req.lot, lot_nos)
        if not recognised:
            _reject(conn, version_id, raw, "lot", f"lot '{req.lot}' is not a lot of this notice", score, pid, str(req.lot))
            stats["rejected"] += 1
            return
        key = (normalise(req.statement), lot_no)
        if key in seen:
            stats["duplicates"] += 1
            return
        seen.add(key)
        checked = check_condition(req.condition, req.quote)
        if checked.note:
            _reject(conn, version_id, raw, "condition", f"requirement kept without its machine check: {checked.note}", score, pid, lot_no)
            stats["conditions_dropped"] += 1
        req_id = conn.execute(
            """INSERT INTO requirement (lot_id, lot_no, version_id, statement, category, obligation, condition,
                                        equivalent_allowed, evidence_needed, quote, passage_id, section, page, refs,
                                        defined_terms, quote_score, scope, condition_note, extract_version, owner_profile_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (lots.get(lot_no) if lot_no else None, lot_no, version_id, req.statement, req.category, req.obligation,
             Jsonb(checked.condition) if checked.condition else None, req.equivalent_allowed, req.evidence_needed, req.quote, pid,
             p["section"], p["page"], req.references, req.defined_terms, score, req.scope, checked.note, EXTRACT_VERSION, p["owner"]),
        ).fetchone()["id"]
        conds = _store_conditions(conn, req_id, req, checked.condition, checked.note, _source_text(p, by_id))
        _record_dependencies(conn, req_id, version_id, req, p, sections, refs_by_label, definitions, conds)
        stats["accepted"] += 1
        stats["ids"].append(req_id)

    for batch in _batches(targets):
        run_batch(batch)
    if only is None:
        conn.execute("UPDATE document SET extracted_at = now() WHERE id = ANY(%s)",
                     (list({p["document_id"] for p in targets}),))
    if commit:
        conn.commit()
    return stats
