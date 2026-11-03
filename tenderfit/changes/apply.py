from dataclasses import asdict

import psycopg
from psycopg.types.json import Jsonb
from rapidfuzz import fuzz

from tenderfit.assessment.pipeline import assess_lot
from tenderfit.changes.diff import Change, PassageChange, align, classify, field_diff
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
    # The public pack only: a supplier's private uploads are not part of what the buyer changed.
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
    labels = sorted({d["label"].lower() for d in change.documents})
    change.document_ref_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM document_ref WHERE version_id = %s AND lower(label) = ANY(%s)", (old_vid, labels))}
    f_old, f_new = (conn.execute("SELECT fields FROM notice_version WHERE id = %s", (v,)).fetchone()["fields"]
                    for v in (old_vid, new_vid))
    change.fields = [f for f in field_diff(f_old, f_new) if f.path != "notice.documents"]
    # Private copies are byte-identical across versions, so requirements read from them carry over unchanged.
    for r in conn.execute(
            """SELECT a.id AS old_doc, b.id AS new_doc, a.filename FROM document a
               JOIN document b ON b.version_id = %s AND b.sha256 = a.sha256 AND b.owner_profile_id = a.owner_profile_id
               WHERE a.version_id = %s AND a.owner_profile_id IS NOT NULL""", (new_vid, old_vid)).fetchall():
        olds = {p["ordinal"]: p for p in conn.execute("SELECT id, ordinal, section FROM passage WHERE document_id = %s", (r["old_doc"],))}
        for n in conn.execute("SELECT id, ordinal FROM passage WHERE document_id = %s", (r["new_doc"],)).fetchall():
            o = olds.get(n["ordinal"])
            if o:
                change.passages.append(PassageChange("unchanged", o["id"], n["id"], o["section"], 100.0, r["filename"] or "upload"))
    mandatory = {r["passage_id"] for r in conn.execute(
        "SELECT passage_id FROM requirement WHERE version_id = %s AND obligation = 'mandatory'", (old_vid,))}
    classify(change, mandatory)
    opp = conn.execute("SELECT opportunity_id FROM notice_version WHERE id = %s", (new_vid,)).fetchone()["opportunity_id"]
    cid = conn.execute(
        """INSERT INTO change_event (opportunity_id, kind, from_version_id, to_version_id, cls, reasons, fields, passages, documents)
           VALUES (%s, 'version', %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (from_version_id, to_version_id) WHERE kind = 'version' DO UPDATE
               SET cls = EXCLUDED.cls, reasons = EXCLUDED.reasons, fields = EXCLUDED.fields, passages = EXCLUDED.passages,
                   documents = EXCLUDED.documents
           RETURNING id""",
        (opp, old_vid, new_vid, change.cls, change.reasons, Jsonb([asdict(f) for f in change.fields]),
         Jsonb([asdict(p) for p in change.passages if p.kind != "unchanged"]), Jsonb(change.documents))).fetchone()["id"]
    # A later notice replaced the target of an unfinished event from the same version: it will never be applied.
    conn.execute("""UPDATE change_event SET status = 'superseded', finished_at = now()
                    WHERE kind = 'version' AND from_version_id = %s AND to_version_id <> %s AND status <> 'applied'""",
                 (old_vid, new_vid))
    conn.commit()
    return cid, change


def _existing_preparation(conn: psycopg.Connection, old_vid: int, new_vid: int) -> dict | None:
    return conn.execute("SELECT added, removed FROM version_preparation WHERE from_version_id = %s AND to_version_id = %s",
                        (old_vid, new_vid)).fetchone()


def _record_preparation(conn: psycopg.Connection, old_vid: int, new_vid: int, added: list, removed: list) -> dict:
    conn.execute("INSERT INTO version_preparation (from_version_id, to_version_id, added, removed) VALUES (%s, %s, %s, %s)",
                 (old_vid, new_vid, Jsonb(added), Jsonb(removed)))
    return {"added": added, "removed": removed}


def prepare_full(conn: psycopg.Connection, old_vid: int, new_vid: int, llm: LLM) -> dict:
    """Extract the whole new version and pair it with the old one, as one transaction ending in a durable marker."""
    done = _existing_preparation(conn, old_vid, new_vid)
    if done:
        return done
    extract_version(conn, new_vid, llm, commit=False)
    # Private requirements stay out of the shared before/after lists; each supplier's run extracts its own.
    added, removed = pair_requirements(
        conn, conn.execute("SELECT * FROM requirement WHERE version_id = %s AND owner_profile_id IS NULL ORDER BY id", (old_vid,)).fetchall(),
        conn.execute("""SELECT * FROM requirement WHERE version_id = %s AND carried_from IS NULL AND owner_profile_id IS NULL
                        ORDER BY id""", (new_vid,)).fetchall())
    out = _record_preparation(conn, old_vid, new_vid, added, removed)
    conn.commit()
    return out


def carry_requirements(conn: psycopg.Connection, old_vid: int, new_vid: int, change: Change, llm: LLM) -> dict:
    """Copy requirements whose source passage is unchanged; re-extract modified and added passages; pair old and new.

    All of it is one transaction: a failed extraction leaves nothing behind, so a retry starts clean."""
    done = _existing_preparation(conn, old_vid, new_vid)
    if done:
        return done
    if conn.execute("SELECT 1 FROM requirement WHERE version_id = %s LIMIT 1", (new_vid,)).fetchone():
        return prepare_full(conn, old_vid, new_vid, llm)  # someone assessed the new version first; nothing to copy
    pmap = change.passage_map
    unchanged = {p.old_id for p in change.passages if p.kind == "unchanged"}
    new_refs = {r["label"].lower(): r["id"] for r in conn.execute("SELECT id, label FROM document_ref WHERE version_id = %s", (new_vid,))}
    old_refs = {r["id"]: r["label"].lower() for r in conn.execute("SELECT id, label FROM document_ref WHERE version_id = %s", (old_vid,))}
    lots = {r["lot_no"]: r["id"] for r in conn.execute(
        "SELECT l.lot_no, l.id FROM lot l JOIN notice_version nv ON nv.opportunity_id = l.opportunity_id WHERE nv.id = %s", (new_vid,))}
    old_reqs = conn.execute("SELECT * FROM requirement WHERE version_id = %s ORDER BY id", (old_vid,)).fetchall()
    displaced = []
    for r in old_reqs:
        if r["passage_id"] not in unchanged:
            displaced.append(r)
            continue
        nid = conn.execute(
            """INSERT INTO requirement (lot_id, lot_no, version_id, statement, category, obligation, condition, equivalent_allowed,
                                        evidence_needed, quote, passage_id, section, page, refs, defined_terms, quote_score, carried_from,
                                        scope, condition_note, extract_version, owner_profile_id)
               SELECT %s, lot_no, %s, statement, category, obligation, condition, equivalent_allowed, evidence_needed, quote, %s,
                      section, page, refs, defined_terms, quote_score, id, scope, condition_note, extract_version, owner_profile_id
               FROM requirement WHERE id = %s RETURNING id""",
            (lots.get(r["lot_no"]) if r["lot_no"] else None, new_vid, pmap[r["passage_id"]], r["id"])).fetchone()["id"]
        conn.execute("""INSERT INTO requirement_condition (requirement_id, ordinal, label, condition, quote, mandatory, evidence_needed, condition_note)
                        SELECT %s, ordinal, label, condition, quote, mandatory, evidence_needed, condition_note
                        FROM requirement_condition WHERE requirement_id = %s""", (nid, r["id"]))
        for d in conn.execute("SELECT * FROM dependency WHERE requirement_id = %s AND verdict_id IS NULL", (r["id"],)).fetchall():
            target = d["target_id"]
            if d["target_type"] == "passage":
                target = str(pmap.get(int(target), target))
            elif d["target_type"] == "document":
                target = str(new_refs.get(old_refs.get(int(target), ""), target))
            conn.execute("""INSERT INTO dependency (requirement_id, version_id, kind, target_type, target_id, recorded_by)
                            VALUES (%s, %s, %s, %s, %s, %s)""", (nid, new_vid, d["kind"], d["target_type"], target, d["recorded_by"]))
    fresh = extract_version(conn, new_vid, llm, only=change.new_passage_ids, commit=False) if change.new_passage_ids else {"ids": []}
    new_reqs = conn.execute("SELECT * FROM requirement WHERE id = ANY(%s)", (fresh.get("ids", []),)).fetchall()
    added, removed = pair_requirements(conn, displaced, new_reqs)
    conn.execute("UPDATE document SET extracted_at = now() WHERE version_id = %s AND access = 'read'", (new_vid,))
    out = _record_preparation(conn, old_vid, new_vid, added, removed)
    conn.commit()
    return out


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
                         static_deps: bool = False, **assess_kw) -> tuple[list[int], list[str]]:
    """Reassess every current assessment on the old version. Each lot and profile succeeds or fails on its own.

    Returns the impacts written and the failures. A pair that already moved to the new version is no longer on the
    old version, so a retry only handles what is left."""
    active = conn.execute(
        """SELECT * FROM assessment WHERE version_id = %s AND NOT superseded AND NOT shadow AND status = 'done' ORDER BY id""",
        (old_vid,)).fetchall()
    full = change.cls in ("structural", "ambiguous")
    prep = prepare_full(conn, old_vid, new_vid, llm) if full else carry_requirements(conn, old_vid, new_vid, change, llm)
    new_reqs = conn.execute("SELECT id, carried_from FROM requirement WHERE version_id = %s", (new_vid,)).fetchall()
    impacts, errors = [], []
    for a in active:
        try:
            plan = plan_reassessment(change, load_deps(conn, a["id"], static_deps), documents=change.document_ref_ids)
            impacts.append(_apply_to_assessment(conn, change_id, change, plan, prep, new_reqs, a, new_vid, llm, assess_kw))
        except Exception as e:
            conn.rollback()
            errors.append(f"lot {a['lot_id']}, profile {a['profile_id']}: {type(e).__name__}: {e}"[:500])
    return impacts, errors


def _apply_to_assessment(conn: psycopg.Connection, change_id: int, change: Change, plan: Plan, prep: dict, new_reqs: list[dict],
                         a: dict, new_vid: int, llm: LLM, assess_kw: dict) -> int:
    before = _verdicts(conn, a["id"])
    affected_old = plan.reassess | plan.rules_only
    carry = {}
    if plan.mode == "targeted":
        carry = {r["id"]: before[r["carried_from"]]["id"] for r in new_reqs
                 if r["carried_from"] in before and r["carried_from"] not in affected_old}
    written: list[int] = []

    def finalise(aid: int) -> None:
        after = _verdicts(conn, aid)
        affected = []
        for rid, v in after.items():
            if v["carried_over"]:
                continue
            old = before.get(v["carried_from"]) if v["carried_from"] else None
            by_rule = v["carried_from"] in plan.rules_only and v["decided_by"] == "rule"
            how = "rule only" if by_rule else ("new requirement" if not old else "re-assessed")
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
        written.append(iid)

    assess_lot(conn, a["lot_id"], a["profile_id"], llm, version_id=new_vid, carry=carry, pmap=change.passage_map, mode=plan.mode,
               reason=plan.reason or f"targeted: change {change_id}", finalise=finalise, **assess_kw)
    return written[0]


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


MAX_EVENT_ATTEMPTS = 5


class ChangeProcessingError(Exception):
    """At least one amendment could not be fully applied; it stays pending, so the job queue retries it."""


AMENDMENT_LOCK = 7002


def pending_version_changes(conn: psycopg.Connection, respect_backoff: bool = False) -> list[tuple[int, int]]:
    """Current assessments behind the notice's current version, unless that event has used up its attempts.

    With respect_backoff, a recently failed event waits, doubling from two minutes, before it counts as pending again."""
    return [(r["old"], r["new"]) for r in conn.execute(
        """SELECT DISTINCT a.version_id AS old, o.current_version_id AS new FROM assessment a
           JOIN lot l ON l.id = a.lot_id JOIN opportunity o ON o.id = l.opportunity_id
           WHERE a.status = 'done' AND NOT a.superseded AND NOT a.shadow AND a.version_id <> o.current_version_id
             AND NOT EXISTS (SELECT 1 FROM change_event c WHERE c.kind = 'version' AND c.from_version_id = a.version_id
                             AND c.to_version_id = o.current_version_id AND c.status = 'failed'
                             AND (c.attempts >= %s OR (%s AND c.finished_at > now() - make_interval(mins => (2 ^ c.attempts)::int))))
           ORDER BY 1, 2""", (MAX_EVENT_ATTEMPTS, respect_backoff))]


def record_status_changes(conn: psycopg.Connection) -> int:
    n = 0
    for r in conn.execute(
            """SELECT DISTINCT o.id, o.status, o.status_reason FROM opportunity o JOIN lot l ON l.opportunity_id = o.id
               JOIN assessment a ON a.lot_id = l.id WHERE a.status = 'done' AND NOT a.superseded AND o.status IN ('cancelled', 'awarded')""").fetchall():
        n += conn.execute(
            """INSERT INTO change_event (opportunity_id, kind, cls, reasons, summary, status)
               VALUES (%s, 'status', 'status', %s, %s, 'applied') ON CONFLICT DO NOTHING""",
            (r["id"], [r["status_reason"]], Jsonb({"status": r["status"]}))).rowcount
    conn.commit()
    return n


def _start_event(conn: psycopg.Connection, change_id: int) -> None:
    conn.execute("""UPDATE change_event SET status = 'processing', attempts = attempts + 1, started_at = now(), finished_at = NULL
                    WHERE id = %s""", (change_id,))
    conn.commit()


def _finish_event(conn: psycopg.Connection, change_id: int, errors: list[str]) -> None:
    conn.execute("UPDATE change_event SET status = %s, last_error = %s, finished_at = now() WHERE id = %s",
                 ("failed" if errors else "applied", "; ".join(errors)[:2000] or None, change_id))
    conn.commit()


def process_pending(conn: psycopg.Connection, llm: LLM, http=None, **assess_kw) -> list[int]:
    """Apply every outstanding amendment. An event is applied only after all its lots and profiles succeeded.

    One process at a time: a second caller, such as a worker alongside a command, leaves the work to the first."""
    if not conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (AMENDMENT_LOCK,)).fetchone()["got"]:
        conn.rollback()
        return []
    conn.commit()
    try:
        return _process_pending(conn, llm, http, assess_kw)
    finally:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (AMENDMENT_LOCK,))
        conn.commit()


def _process_pending(conn: psycopg.Connection, llm: LLM, http, assess_kw: dict) -> list[int]:
    out, failed = [], []
    for old, new in pending_version_changes(conn):
        cid = None
        try:
            cid, change = detect(conn, old, new, http)
            _start_event(conn, cid)
            impacts, errors = apply_version_change(conn, cid, change, old, new, llm, http=http, **assess_kw)
            out += impacts
        except Exception as e:
            conn.rollback()
            errors = [f"{type(e).__name__}: {e}"]
            if cid is None:
                failed.append(f"versions {old}->{new}: {errors[0]}")
                continue
        _finish_event(conn, cid, errors)
        if errors:
            failed.append(f"change {cid}: {errors[0]}")
    record_status_changes(conn)
    if failed:
        raise ChangeProcessingError("; ".join(failed))
    return out


def retry_event(conn: psycopg.Connection, change_id: int) -> bool:
    """A person asks for another try after the attempts ran out."""
    row = conn.execute("""UPDATE change_event SET status = 'detected', attempts = 0 WHERE id = %s AND kind = 'version'
                          AND status = 'failed' RETURNING id""", (change_id,)).fetchone()
    if row and not queue.pending(conn, "detect_changes"):
        queue.enqueue(conn, "detect_changes", lock_key="changes")
    conn.commit()
    return bool(row)


def apply_profile_change(conn: psycopg.Connection, lot_id: int, profile_id: int, fact_ids: list[int], llm: LLM,
                         reason: str, **assess_kw) -> int | None:
    a = conn.execute("""SELECT * FROM assessment WHERE lot_id = %s AND profile_id = %s AND NOT superseded AND NOT shadow
                        AND status = 'done' ORDER BY id DESC LIMIT 1""", (lot_id, profile_id)).fetchone()
    if not a:
        return None
    change = Change(cls="local")
    plan = plan_reassessment(change, load_deps(conn, a["id"]), facts=set(fact_ids))
    before = _verdicts(conn, a["id"])
    affected_ids = plan.reassess | plan.rules_only
    carry = {rid: v["id"] for rid, v in before.items() if rid not in affected_ids}
    opp = conn.execute("SELECT opportunity_id FROM lot WHERE id = %s", (lot_id,)).fetchone()["opportunity_id"]

    def finalise(aid: int) -> None:
        # The event and its impact appear only together with the assessment they describe.
        cid = conn.execute(
            """INSERT INTO change_event (opportunity_id, kind, from_version_id, to_version_id, cls, reasons, summary, status, attempts)
               VALUES (%s, 'profile', %s, %s, 'local', %s, %s, 'applied', 1) RETURNING id""",
            (opp, a["version_id"], a["version_id"], [reason], Jsonb({"facts": fact_ids, "profile_id": profile_id}))).fetchone()["id"]
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

    return assess_lot(conn, lot_id, profile_id, llm, version_id=a["version_id"], carry=carry, mode="targeted",
                      reason=f"profile change: {reason}", finalise=finalise, **assess_kw)
