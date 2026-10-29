"""Points where a person must confirm (Table 13). Every confirmation is stored with who, when and what it changed."""
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from tenderfit.profile import service as profiles

POINTS = {
    "G1": {"target": "lot", "decisions": {"pursue", "not_for_us"}},
    "G2": {"target": "verdict", "decisions": {"confirm_declaration"}},
    "G3": {"target": "verdict", "decisions": {"confirm_for_lot"}},
    "G4": {"target": "fact", "decisions": {"renewed", "reconfirmed"}},
    "G5": {"target": "verdict", "decisions": {"clarification_reviewed", "clarification_sent"}},
    "G6": {"target": "verdict", "decisions": {"override", "accept_blocker"}},
    "G7": {"target": "document_ref", "decisions": {"none_issued"}},
}


class ConfirmationError(ValueError):
    pass


def _verdict(conn: psycopg.Connection, verdict_id: int) -> dict:
    v = conn.execute(
        """SELECT v.*, a.lot_id, a.profile_id FROM verdict v JOIN assessment a ON a.id = v.assessment_id WHERE v.id = %s""",
        (verdict_id,)).fetchone()
    if not v:
        raise ConfirmationError("verdict not found")
    return v


def confirm(conn: psycopg.Connection, point: str, user: str, target_id: int, decision: str, reason: str | None = None,
            profile_id: int | None = None, payload: dict | None = None) -> dict:
    spec = POINTS.get(point)
    if not spec or decision not in spec["decisions"]:
        raise ConfirmationError(f"{point} accepts {sorted(spec['decisions']) if spec else 'nothing'}")
    payload = payload or {}
    reassess: list[tuple[int, int]] = []

    if point == "G1":
        if not profile_id:
            raise ConfirmationError("profile_id required")
        conn.execute(
            """INSERT INTO lot_decision (lot_id, profile_id, decision, decided_by, reason) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (lot_id, profile_id) DO UPDATE SET decision = EXCLUDED.decision, decided_by = EXCLUDED.decided_by,
               reason = EXCLUDED.reason, created_at = now()""", (target_id, profile_id, decision, user, reason))
        if decision == "pursue":
            reassess.append((target_id, profile_id))
    elif point in ("G2", "G3"):
        v = _verdict(conn, target_id)
        profile_id = v["profile_id"]
        kind = "declaration" if point == "G2" else "workload"
        facts = [r["fact_id"] for r in conn.execute("SELECT fact_id FROM verdict_fact WHERE verdict_id = %s", (target_id,))]
        if not facts:
            raise ConfirmationError("this verdict uses no profile fact to confirm")
        for f in facts:
            conn.execute("""INSERT INTO lot_fact_confirmation (lot_id, profile_id, fact_id, kind, confirmed_by)
                            VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""", (v["lot_id"], profile_id, f, kind, user))
        payload["facts"] = facts
        reassess.append((v["lot_id"], profile_id))
    elif point == "G4":
        changes = {k: date.fromisoformat(payload[k]) for k in ("valid_until", "confirmed_at", "confirm_by") if payload.get(k)}
        if decision == "reconfirmed" and "confirmed_at" not in changes:
            changes["confirmed_at"] = date.today()
        status = conn.execute("SELECT evidence_status, source FROM profile_fact WHERE id = %s", (target_id,)).fetchone()
        if not status:
            raise ConfirmationError("fact not found")
        restored = "document_on_file" if payload.get("evidence_file_id") else (
            "declared" if status["evidence_status"] == "needs_update" else status["evidence_status"])
        verdicts = profiles.update_fact(conn, target_id, evidence_status=restored, **changes)
        profile_id = conn.execute("SELECT profile_id FROM profile_fact WHERE id = %s", (target_id,)).fetchone()["profile_id"]
        reassess += [(r["lot_id"], r["profile_id"]) for r in conn.execute(
            "SELECT DISTINCT a.lot_id, a.profile_id FROM verdict v JOIN assessment a ON a.id = v.assessment_id WHERE v.id = ANY(%s)",
            (verdicts,))]
    elif point == "G5":
        v = _verdict(conn, target_id)
        profile_id = v["profile_id"]
    elif point == "G6":
        v = _verdict(conn, target_id)
        profile_id = v["profile_id"]
        if v["status"] != "not_met":
            raise ConfirmationError("G6 applies to a NOT MET verdict")
        if decision == "override" and not (reason and reason.strip()):
            raise ConfirmationError("an override needs a written reason")
        if decision == "override":
            conn.execute("UPDATE verdict SET override_status = 'needs_confirmation', override_reason = %s, override_by = %s "
                         "WHERE id = %s", (reason, user, target_id))
            payload["requirement_id"] = v["requirement_id"]
            reassess.append((v["lot_id"], profile_id))
    elif point == "G7":
        ref = conn.execute("SELECT * FROM document_ref WHERE id = %s", (target_id,)).fetchone()
        if not ref or ref["kind"] == "notice":
            raise ConfirmationError("document not found")
        conn.execute("UPDATE document_ref SET status = 'confirmed_none', note = %s WHERE id = %s",
                     (f"buyer issued none (confirmed by {user})", target_id))
        reassess += [(r["lot_id"], r["profile_id"]) for r in conn.execute(
            """SELECT DISTINCT a.lot_id, a.profile_id FROM assessment a WHERE a.version_id = %s AND NOT a.superseded""",
            (ref["version_id"],))]

    cid = conn.execute(
        """INSERT INTO confirmation (point, user_name, target_type, target_id, decision, reason, profile_id, payload)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (point, user, spec["target"], target_id, decision, reason, profile_id, Jsonb(payload))).fetchone()["id"]
    conn.commit()
    return {"confirmation_id": cid, "reassess": sorted(set(reassess))}


def overrides(conn: psycopg.Connection, profile_id: int) -> dict[int, dict]:
    return {r["requirement_id"]: r for r in conn.execute(
        """SELECT DISTINCT ON ((payload->>'requirement_id')::bigint) (payload->>'requirement_id')::bigint AS requirement_id,
                  reason, user_name FROM confirmation
           WHERE point = 'G6' AND decision = 'override' AND profile_id = %s ORDER BY (payload->>'requirement_id')::bigint, id DESC""",
        (profile_id,))}
