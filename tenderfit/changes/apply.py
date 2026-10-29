from dataclasses import asdict

import psycopg
from psycopg.types.json import Jsonb
from rapidfuzz import fuzz

from tenderfit.assessment.pipeline import assess_lot
from tenderfit.changes.diff import Change, align, classify, field_diff
from tenderfit.changes.impact import Plan, load_deps, plan_reassessment
from tenderfit.documents import service as docs
from tenderfit.documents.text import normalise
from tenderfit.extraction.extract import extract_version
from tenderfit.jobs import queue
from tenderfit.llm import LLM


def _passages(conn: psycopg.Connection, document_id: int) -> list[dict]:
    return conn.execute("SELECT id, sha256, section, text FROM passage WHERE document_id = %s ORDER BY ordinal",
                        (document_id,)).fetchall()


def _ref_key(r: dict) -> tuple:
    return (r["kind"], r["label"].lower(), r["ocds_type"])


def detect(conn: psycopg.Connection, old_vid: int, new_vid: int, http=None) -> tuple[int, Change]:
    docs.process_version(conn, old_vid, http)
    docs.process_version(conn, new_vid, http)
    old_refs = {_ref_key(r): r for r in docs.inventory(conn, old_vid)["documents"] if r["kind"] != "referenced"}
    new_refs = {_ref_key(r): r for r in docs.inventory(conn, new_vid)["documents"] if r["kind"] != "referenced"}
    change = Change()
    for key in sorted(set(old_refs) | set(new_refs), key=str):
        o, n = old_refs.get(key), new_refs.get(key)
        label = (n or o)["label"]
        if o and n:
            if n["status"] == "unreadable" and o["status"] == "read":
                change.documents.append({"kind": "unreadable", "label": label, "essential": n["essential"]})
            if o["status"] == "read" and n["status"] == "read":
                change.passages += align(_passages(conn, o["document_id"]), _passages(conn, n["document_id"]), label)
            elif o["status"] != n["status"]:
                change.documents.append({"kind": "added" if n["status"] == "read" else "removed", "label": label,
                                         "essential": n["essential"]})
                if n["status"] == "read":
                    change.passages += align([], _passages(conn, n["document_id"]), label)
        elif n:
            change.documents.append({"kind": "added", "label": label, "essential": n["essential"]})
            if n["status"] == "read":
                change.passages += align([], _passages(conn, n["document_id"]), label)
            elif n["status"] == "unreadable":
                change.documents.append({"kind": "unreadable", "label": label, "essential": n["essential"]})
        else:
            change.documents.append({"kind": "removed", "label": label, "essential": o["essential"]})
            if o["status"] == "read":
                change.passages += align(_passages(conn, o["document_id"]), [], label)
    f_old, f_new = (conn.execute("SELECT fields FROM notice_version WHERE id = %s", (v,)).fetchone()["fields"]
                    for v in (old_vid, new_vid))
    change.fields = [f for f in field_diff(f_old, f_new) if f.path != "notice.documents"]
    mandatory = {r["passage_id"] for r in conn.execute(
        "SELECT passage_id FROM requirement WHERE version_id = %s AND obligation = 'mandatory'", (old_vid,))}
    classify(change, mandatory)
    opp = conn.execute("SELECT opportunity_id FROM notice_version WHERE id = %s", (new_vid,)).fetchone()["opportunity_id"]
    cid = conn.execute(
        """INSERT INTO change_event (opportunity_id, kind, from_version_id, to_version_id, cls, reasons, fields, passages, documents)
           VALUES (%s, 'version', %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (from_version_id, to_version_id) WHERE kind = 'version' DO UPDATE SET detected_at = now()
           RETURNING id""",
        (opp, old_vid, new_vid, change.cls, change.reasons, Jsonb([asdict(f) for f in change.fields]),
         Jsonb([asdict(p) for p in change.passages if p.kind != "unchanged"]), Jsonb(change.documents))).fetchone()["id"]
    conn.commit()
    return cid, change


def carry_requirements(conn: psycopg.Connection, old_vid: int, new_vid: int, change: Change, llm: LLM) -> dict:
    """Copy requirements whose source passage is unchanged; re-extract modified and added passages; pair old and new."""
    if conn.execute("SELECT 1 FROM requirement WHERE version_id = %s LIMIT 1", (new_vid,)).fetchone():
        return {"prepared": False}
    pmap = change.passage_map
    unchanged = {p.old_id for p in change.passages if p.kind == "unchanged"}
    new_refs = {r["label"].lower(): r["id"] for r in conn.execute("SELECT id, label FROM document_ref WHERE version_id = %s", (new_vid,))}
    old_refs = {r["id"]: r["label"].lower() for r in conn.execute("SELECT id, label FROM document_ref WHERE version_id = %s", (old_vid,))}
    lots = {r["lot_no"]: r["id"] for r in conn.execute(
        "SELECT l.lot_no, l.id FROM lot l JOIN notice_version nv ON nv.opportunity_id = l.opportunity_id WHERE nv.id = %s", (new_vid,))}
    old_reqs = conn.execute("SELECT * FROM requirement WHERE version_id = %s ORDER BY id", (old_vid,)).fetchall()
    mapping, displaced = {}, []
    for r in old_reqs:
        if r["passage_id"] not in unchanged:
            displaced.append(r)
            continue
        nid = conn.execute(
            """INSERT INTO requirement (lot_id, lot_no, version_id, statement, category, obligation, condition, equivalent_allowed,
                                        evidence_needed, quote, passage_id, section, page, refs, defined_terms, quote_score, carried_from)
               SELECT %s, lot_no, %s, statement, category, obligation, condition, equivalent_allowed, evidence_needed, quote, %s,
                      section, page, refs, defined_terms, quote_score, id FROM requirement WHERE id = %s RETURNING id""",
            (lots.get(r["lot_no"]) if r["lot_no"] else None, new_vid, pmap[r["passage_id"]], r["id"])).fetchone()["id"]
        mapping[r["id"]] = nid
        for d in conn.execute("SELECT * FROM dependency WHERE requirement_id = %s AND verdict_id IS NULL", (r["id"],)).fetchall():
            target = d["target_id"]
            if d["target_type"] == "passage":
                target = str(pmap.get(int(target), target))
            elif d["target_type"] == "document":
                target = str(new_refs.get(old_refs.get(int(target), ""), target))
            conn.execute("""INSERT INTO dependency (requirement_id, version_id, kind, target_type, target_id, recorded_by)
                            VALUES (%s, %s, %s, %s, %s, %s)""", (nid, new_vid, d["kind"], d["target_type"], target, d["recorded_by"]))
    conn.commit()
    fresh = extract_version(conn, new_vid, llm, only=change.new_passage_ids) if change.new_passage_ids else {"ids": []}
    new_reqs = conn.execute("SELECT * FROM requirement WHERE id = ANY(%s)", (fresh.get("ids", []),)).fetchall()
    added, removed = pair_requirements(conn, displaced, new_reqs)
    conn.commit()
    return {"prepared": True, "carried": len(mapping), "added": added, "removed": removed}


def pair_requirements(conn: psycopg.Connection, old: list[dict], new: list[dict]) -> tuple[list[dict], list[dict]]:
    """Link re-extracted requirements to the ones they replace (carried_from); return introduced and removed ones."""
    used, removed = set(), []
    for o in old:
        score = lambda n: fuzz.token_set_ratio(normalise(o["statement"]), normalise(n["statement"]))  # noqa: E731
        cands = [n for n in new if n["id"] not in used and n["lot_no"] == o["lot_no"] and n["category"] == o["category"]]
        best = max(cands, key=score, default=None)
        if best and score(best) >= 70:
            used.add(best["id"])
            conn.execute("UPDATE requirement SET carried_from = %s WHERE id = %s", (o["id"], best["id"]))
        else:
            removed.append({"id": o["id"], "statement": o["statement"], "obligation": o["obligation"]})
    added = [{"id": n["id"], "statement": n["statement"], "obligation": n["obligation"]} for n in new if n["id"] not in used]
    return added, removed


def _verdicts(conn: psycopg.Connection, aid: int) -> dict[int, dict]:
    return {r["requirement_id"]: r for r in conn.execute(
        """SELECT v.*, r.statement, r.carried_from, r.obligation FROM verdict v JOIN requirement r ON r.id = v.requirement_id
           WHERE v.assessment_id = %s""", (aid,))}


def explain(change: Change, plan: Plan, before: dict, after: dict, rec_before: str | None, rec_after: str | None,
            kept: int, affected: list[dict]) -> str:
    lines = []
    for f in change.fields:
        lines.append(f"{f.path}: {f.before} → {f.after}")
    for p in change.passages:
        if p.kind != "unchanged":
            lines.append(f"{p.document} {p.section or ''}: passage {p.kind}" + (f" ({p.confidence:.0f}% aligned)" if p.kind == "modified" else ""))
    for d in change.documents:
        lines.append(f"document {d['kind']}: {d['label']}")
    if plan.mode == "full":
        head = f"Full reassessment of the lot: {plan.reason}."
    else:
        n_rules = sum(1 for a in affected if a["how"] == "rule only")
        head = (f"Re-checked only what the change touches: {len(affected)} requirement{'s' if len(affected) != 1 else ''} "
                f"re-assessed ({n_rules} by rule, no model call), {kept} verdict{'s' if kept != 1 else ''} kept.")
    for a in affected:
        lines.append(f"{a['statement']}: {a['before'] or 'new'} → {a['after']} ({a['reason']})")
    if rec_before != rec_after:
        lines.append(f"Recommendation: {rec_before} → {rec_after}")
    return head + "\n" + "\n".join(f"- {line}" for line in lines)


def apply_version_change(conn: psycopg.Connection, change_id: int, change: Change, old_vid: int, new_vid: int, llm: LLM,
                         **assess_kw) -> list[int]:
    active = conn.execute(
        """SELECT * FROM assessment WHERE version_id = %s AND NOT superseded AND NOT shadow AND status = 'done'""",
        (old_vid,)).fetchall()
    deps = load_deps(conn, old_vid)
    plan = plan_reassessment(change, deps)
    if plan.mode == "full":
        if not conn.execute("SELECT 1 FROM requirement WHERE version_id = %s LIMIT 1", (new_vid,)).fetchone():
            extract_version(conn, new_vid, llm)
            added, removed = pair_requirements(
                conn, conn.execute("SELECT * FROM requirement WHERE version_id = %s ORDER BY id", (old_vid,)).fetchall(),
                conn.execute("SELECT * FROM requirement WHERE version_id = %s ORDER BY id", (new_vid,)).fetchall())
            conn.commit()
            prep = {"added": added, "removed": removed}
        else:
            prep = {"added": [], "removed": []}
    else:
        prep = carry_requirements(conn, old_vid, new_vid, change, llm)
    new_reqs = conn.execute("SELECT id, carried_from FROM requirement WHERE version_id = %s", (new_vid,)).fetchall()
    impacts = []
    for a in active:
        before = _verdicts(conn, a["id"])
        affected_old = plan.reassess | plan.rules_only
        carry = {}
        if plan.mode == "targeted":
            carry = {r["id"]: before[r["carried_from"]]["id"] for r in new_reqs
                     if r["carried_from"] in before and r["carried_from"] not in affected_old
                     and r["carried_from"] not in plan.reassess}
        aid = assess_lot(conn, a["lot_id"], a["profile_id"], llm, version_id=new_vid, carry=carry, pmap=change.passage_map,
                         mode=plan.mode, reason=plan.reason or f"targeted: change {change_id}", **assess_kw)
        after = _verdicts(conn, aid)
        affected = []
        for rid, v in after.items():
            if v["carried_over"]:
                continue
            old = before.get(v["carried_from"]) if v["carried_from"] else None
            how = "rule only" if v["carried_from"] in plan.rules_only else ("new requirement" if not old else "re-assessed")
            if plan.mode == "full" and old and old["status"] == v["status"]:
                continue
            affected.append({"requirement_id": rid, "statement": v["statement"], "before": old["status"] if old else None,
                             "after": v["status"], "reason": v["reason"], "how": how})
        kept = sum(1 for v in after.values() if v["carried_over"])
        recs = conn.execute("SELECT id, recommendation FROM assessment WHERE id = ANY(%s)", ([a["id"], aid],)).fetchall()
        rec = {r["id"]: r["recommendation"] for r in recs}
        text = explain(change, plan, before, after, rec.get(a["id"]), rec.get(aid), kept, affected)
        iid = conn.execute(
            """INSERT INTO change_impact (change_id, lot_id, profile_id, before_assessment_id, after_assessment_id, mode, reason,
                                          affected, requirements_added, requirements_removed, kept, explanation)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (change_id, a["lot_id"], a["profile_id"], a["id"], aid, plan.mode, plan.reason, Jsonb(affected),
             Jsonb(prep.get("added", [])), Jsonb(prep.get("removed", [])), kept, text)).fetchone()["id"]
        pursued = conn.execute("SELECT 1 FROM lot_decision WHERE lot_id = %s AND profile_id = %s AND decision = 'pursue'",
                               (a["lot_id"], a["profile_id"])).fetchone()
        if plan.mode == "targeted" and pursued:
            queue.enqueue(conn, "shadow_full", {"impact_id": iid}, lock_key=f"lot:{a['lot_id']}")
        conn.commit()
        impacts.append(iid)
    return impacts


def shadow_full(conn: psycopg.Connection, impact_id: int, llm: LLM, **assess_kw) -> dict:
    """Full reassessment in the background for a pursued lot; any mandatory disagreement is a missed dependency."""
    imp = conn.execute("SELECT * FROM change_impact WHERE id = %s", (impact_id,)).fetchone()
    targeted = conn.execute("SELECT * FROM assessment WHERE id = %s", (imp["after_assessment_id"],)).fetchone()
    sid = assess_lot(conn, imp["lot_id"], imp["profile_id"], llm, version_id=targeted["version_id"], mode="full",
                     reason="background full reassessment of a pursued lot", shadow=True, **assess_kw)
    t, s = _verdicts(conn, targeted["id"]), _verdicts(conn, sid)
    diffs = [{"requirement_id": rid, "statement": v["statement"], "targeted": v["status"], "full": s[rid]["status"]}
             for rid, v in t.items() if rid in s and v["obligation"] == "mandatory" and s[rid]["status"] != v["status"]]
    audit = {"agrees": not diffs, "missed_dependencies": diffs}
    conn.execute("UPDATE change_impact SET shadow_assessment_id = %s, audit = %s WHERE id = %s", (sid, Jsonb(audit), impact_id))
    conn.commit()
    return audit


def pending_version_changes(conn: psycopg.Connection) -> list[tuple[int, int]]:
    return [(r["old"], r["new"]) for r in conn.execute(
        """SELECT DISTINCT a.version_id AS old, o.current_version_id AS new FROM assessment a
           JOIN lot l ON l.id = a.lot_id JOIN opportunity o ON o.id = l.opportunity_id
           WHERE NOT a.superseded AND NOT a.shadow AND a.version_id <> o.current_version_id
             AND NOT EXISTS (SELECT 1 FROM change_event c WHERE c.kind = 'version' AND c.from_version_id = a.version_id
                             AND c.to_version_id = o.current_version_id)""")]


def record_status_changes(conn: psycopg.Connection) -> int:
    n = 0
    for r in conn.execute(
            """SELECT DISTINCT o.id, o.status, o.status_reason FROM opportunity o JOIN lot l ON l.opportunity_id = o.id
               JOIN assessment a ON a.lot_id = l.id WHERE NOT a.superseded AND o.status IN ('cancelled', 'awarded')""").fetchall():
        n += conn.execute(
            """INSERT INTO change_event (opportunity_id, kind, cls, reasons, summary) VALUES (%s, 'status', 'status', %s, %s)
               ON CONFLICT DO NOTHING""",
            (r["id"], [r["status_reason"]], Jsonb({"status": r["status"]}))).rowcount
    conn.commit()
    return n


def process_pending(conn: psycopg.Connection, llm: LLM, http=None, **assess_kw) -> list[int]:
    out = []
    for old, new in pending_version_changes(conn):
        cid, change = detect(conn, old, new, http)
        out += apply_version_change(conn, cid, change, old, new, llm, **assess_kw)
    record_status_changes(conn)
    return out


def apply_profile_change(conn: psycopg.Connection, lot_id: int, profile_id: int, fact_ids: list[int], llm: LLM,
                         reason: str, **assess_kw) -> int | None:
    a = conn.execute("""SELECT * FROM assessment WHERE lot_id = %s AND profile_id = %s AND NOT superseded AND NOT shadow
                        AND status = 'done' ORDER BY id DESC LIMIT 1""", (lot_id, profile_id)).fetchone()
    if not a:
        return None
    change = Change(cls="local")
    plan = plan_reassessment(change, load_deps(conn, a["version_id"]), facts=set(fact_ids))
    before = _verdicts(conn, a["id"])
    affected_ids = plan.reassess | plan.rules_only
    carry = {rid: v["id"] for rid, v in before.items() if rid not in affected_ids}
    opp = conn.execute("SELECT opportunity_id FROM lot WHERE id = %s", (lot_id,)).fetchone()["opportunity_id"]
    cid = conn.execute(
        """INSERT INTO change_event (opportunity_id, kind, from_version_id, to_version_id, cls, reasons, summary)
           VALUES (%s, 'profile', %s, %s, 'local', %s, %s) RETURNING id""",
        (opp, a["version_id"], a["version_id"], [reason], Jsonb({"facts": fact_ids, "profile_id": profile_id}))).fetchone()["id"]
    aid = assess_lot(conn, lot_id, profile_id, llm, version_id=a["version_id"], carry=carry, mode="targeted",
                     reason=f"profile change: {reason}", **assess_kw)
    after = _verdicts(conn, aid)
    affected = [{"requirement_id": rid, "statement": v["statement"], "before": before[rid]["status"] if rid in before else None,
                 "after": v["status"], "reason": v["reason"], "how": "fact used"}
                for rid, v in after.items() if not v["carried_over"]]
    recs = {r["id"]: r["recommendation"] for r in conn.execute(
        "SELECT id, recommendation FROM assessment WHERE id = ANY(%s)", ([a["id"], aid],))}
    kept = sum(v["carried_over"] for v in after.values())
    text = explain(change, plan, before, after, recs[a["id"]], recs[aid], kept, affected)
    conn.execute(
        """INSERT INTO change_impact (change_id, lot_id, profile_id, before_assessment_id, after_assessment_id, mode, reason,
                                      affected, kept, explanation) VALUES (%s, %s, %s, %s, %s, 'targeted', %s, %s, %s, %s)""",
        (cid, lot_id, profile_id, a["id"], aid, reason, Jsonb(affected), kept, text))
    conn.commit()
    return aid
