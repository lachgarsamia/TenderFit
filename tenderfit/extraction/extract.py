import re

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from tenderfit.documents import references
from tenderfit.documents.text import QUOTE_THRESHOLD, normalise, quote_score, words
from tenderfit.extraction.prompts import EXTRACT_SYSTEM, EXTRACT_VERSION, extract_user
from tenderfit.extraction.schema import EXTRACTION_SCHEMA, Requirement
from tenderfit.llm import LLM, LLMError, log_call

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
    if best[1] >= QUOTE_THRESHOLD:
        return best
    for p in by_id.values():
        if p["document_id"] == cited["document_id"] and abs(p["ordinal"] - cited["ordinal"]) <= 1 and p["id"] != cited["id"]:
            joined = cited["text"] + " " + p["text"] if p["ordinal"] > cited["ordinal"] else p["text"] + " " + cited["text"]
            s = quote_score(req.quote, joined)
            if s > best[1]:
                best = (cited["id"], s)
    return best if best[1] >= QUOTE_THRESHOLD else (None, best[1])


def _record_dependencies(conn: psycopg.Connection, req_id: int, version_id: int, req: Requirement, passage: dict,
                         sections: dict[str, int], refs_by_label: dict[str, int], definitions: dict[str, int]) -> None:
    deps = [("source", "passage", passage["id"])]
    labels = set(req.references) | {r.label for r in references.find(passage["text"])}
    for label in labels:
        low = label.lower()
        target = next((pid for sec, pid in sections.items() if sec.startswith(low.replace("section ", ""))
                       or sec.startswith(low)), None)
        if target:
            deps.append(("reference", "passage", target))
        elif low in refs_by_label:
            deps.append(("reference", "document", refs_by_label[low]))
    for term in req.defined_terms:
        pid = definitions.get(term.lower())
        deps.append(("definition", "passage", pid) if pid else ("definition", "field", f"term:{term.lower()}"))
    if req.condition:
        for v in (req.condition.bound, req.condition.anchor, req.condition.min_units):
            if isinstance(v, str) and v.split(".")[0] in ("notice", "lot"):
                deps.append(("parameter", "field", v))
    for kind, ttype, tid in dict.fromkeys(deps):
        conn.execute(
            """INSERT INTO dependency (requirement_id, version_id, kind, target_type, target_id, recorded_by)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (req_id, version_id, kind, ttype, str(tid), "rule_engine" if kind == "parameter" else "extractor"))


def _passages(conn: psycopg.Connection, version_id: int) -> list[dict]:
    return conn.execute(
        """SELECT p.id, p.document_id, p.ordinal, p.page, p.section, p.text, coalesce(d.filename, 'document') AS document
           FROM passage p JOIN document d ON d.id = p.document_id
           WHERE d.version_id = %s AND d.access = 'read' ORDER BY d.id, p.ordinal""", (version_id,)).fetchall()


def extract_version(conn: psycopg.Connection, version_id: int, llm: LLM, force: bool = False,
                    only: set[int] | None = None) -> dict:
    if only is None and not force and conn.execute(
            "SELECT 1 FROM requirement WHERE version_id = %s LIMIT 1", (version_id,)).fetchone():
        return {"cached": True}
    passages = _passages(conn, version_id)
    by_id = {p["id"]: p for p in passages}
    targets = [p for p in passages if only is None or p["id"] in only]
    lots = {r["lot_no"]: r["id"] for r in conn.execute(
        """SELECT l.lot_no, l.id FROM lot l JOIN notice_version nv ON nv.opportunity_id = l.opportunity_id
           WHERE nv.id = %s""", (version_id,))}
    sections: dict[str, int] = {}
    for p in passages:
        if p["section"]:
            sections.setdefault(p["section"].lower(), p["id"])
    definitions = {}
    for p in passages:
        if p["section"] and "defin" in p["section"].lower():
            for term in set(t for t in re.findall(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)*)\b", p["text"])):
                definitions.setdefault(term.lower(), p["id"])
    refs_by_label = {r["label"].lower(): r["id"] for r in conn.execute(
        "SELECT id, label FROM document_ref WHERE version_id = %s", (version_id,))}

    stats = {"accepted": 0, "rejected": 0, "duplicates": 0, "calls": 0, "cost_usd": 0.0}
    seen: set[tuple] = set()
    stats["ids"] = []
    for batch in _batches(targets):
        try:
            res = llm.json("extract", EXTRACT_SYSTEM, extract_user(batch, sorted(lots)), EXTRACTION_SCHEMA)
        except LLMError:
            conn.rollback()
            raise
        stats["calls"] += 1
        stats["cost_usd"] += res.usage.cost_usd
        log_call(conn, "extract", EXTRACT_VERSION, res.usage, {"version_id": version_id,
                                                                "passages": [p["id"] for p in batch]})
        for raw in res.data.get("requirements", []):
            try:
                req = Requirement(**raw)
            except ValidationError as e:
                conn.execute("INSERT INTO requirement_rejection (version_id, statement, quote, reason) VALUES (%s, %s, %s, %s)",
                             (version_id, raw.get("statement"), raw.get("quote"), f"invalid: {e.errors()[0]['msg']}"))
                stats["rejected"] += 1
                continue
            pid, score = verify(req, by_id)
            if pid is None:
                conn.execute(
                    """INSERT INTO requirement_rejection (version_id, passage_id, statement, quote, score, reason)
                       VALUES (%s, %s, %s, %s, %s, 'quote not found in source passage')""",
                    (version_id, req.passage_id if req.passage_id in by_id else None, req.statement, req.quote, score))
                stats["rejected"] += 1
                continue
            key = (normalise(req.statement), req.lot)
            if key in seen:
                stats["duplicates"] += 1
                continue
            seen.add(key)
            p = by_id[pid]
            req_id = conn.execute(
                """INSERT INTO requirement (lot_id, lot_no, version_id, statement, category, obligation, condition,
                                            equivalent_allowed, evidence_needed, quote, passage_id, section, page, refs,
                                            defined_terms, quote_score)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (lots.get(req.lot) if req.lot else None, req.lot, version_id, req.statement, req.category,
                 req.obligation, Jsonb(req.condition.model_dump()) if req.condition else None, req.equivalent_allowed,
                 req.evidence_needed, req.quote, pid, p["section"], p["page"], req.references, req.defined_terms, score),
            ).fetchone()["id"]
            _record_dependencies(conn, req_id, version_id, req, p, sections, refs_by_label, definitions)
            stats["accepted"] += 1
            stats["ids"].append(req_id)
    conn.commit()
    return stats
