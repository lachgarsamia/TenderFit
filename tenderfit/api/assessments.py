from datetime import date, datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from tenderfit.assessment.confirm import ConfirmationError, confirm
from tenderfit.db import connect
from tenderfit.documents import service as docs
from tenderfit.jobs import queue
from tenderfit.profile import service as profiles

router = APIRouter()


def enqueue_assess(conn, pairs: list[tuple[int, int]], reason: str) -> list[int]:
    ids = [queue.enqueue(conn, "assess", {"lot_id": lot, "profile_id": prof, "reason": reason}, lock_key=f"lot:{lot}")
           for lot, prof in sorted(set(pairs))]
    conn.commit()
    return ids


def enqueue_profile_change(conn, pairs: list[tuple[int, int]], facts: list[int], reason: str) -> list[int]:
    ids = [queue.enqueue(conn, "profile_change", {"lot_id": lot, "profile_id": prof, "facts": facts, "reason": reason},
                         lock_key=f"lot:{lot}") for lot, prof in sorted(set(pairs))]
    conn.commit()
    return ids


def _lot(conn, lot_id: int) -> dict:
    lot = conn.execute(
        """SELECT l.*, o.id AS opportunity_id, o.ocid, o.title AS opportunity_title, o.current_version_id, o.status AS opp_status,
                  o.status_reason, o.last_checked_at, o.deadline, b.name AS buyer
           FROM lot l JOIN opportunity o ON o.id = l.opportunity_id LEFT JOIN buyer b ON b.id = o.buyer_id WHERE l.id = %s""",
        (lot_id,)).fetchone()
    if not lot:
        raise HTTPException(404, "lot not found")
    return lot


@router.get("/profiles")
def list_profiles():
    with connect() as conn:
        return conn.execute("SELECT id, name, regions, description, is_fictional FROM business_profile ORDER BY id").fetchall()


@router.get("/lots/{lot_id}/assessment")
def get_assessment(lot_id: int, profile_id: int):
    from tenderfit.api.main import effective_status

    with connect() as conn:
        lot = _lot(conn, lot_id)
        a = conn.execute("""SELECT * FROM assessment WHERE lot_id = %s AND profile_id = %s AND NOT superseded
                            ORDER BY id DESC LIMIT 1""", (lot_id, profile_id)).fetchone()
        if not a:
            raise HTTPException(404, "no assessment yet; POST /lots/{id}/assess")
        verdicts = conn.execute(
            """SELECT v.id, v.status, v.reason, v.basis, v.needs_human, v.provisional, v.question, v.escalate,
                      v.escalate_reason, v.decided_by, v.override_status, v.override_reason, v.override_by,
                      r.id AS requirement_id, r.statement, r.category, r.obligation, r.quote, r.section, r.page,
                      r.equivalent_allowed, coalesce(d.filename, 'document') AS document,
                      (SELECT coalesce(jsonb_agg(jsonb_build_object('passage_id', c.passage_id, 'quote', c.quote,
                              'section', p.section, 'page', p.page)), '[]')
                         FROM verdict_citation c JOIN passage p ON p.id = c.passage_id WHERE c.verdict_id = v.id) AS citations,
                      (SELECT coalesce(jsonb_agg(jsonb_build_object('id', f.id, 'label', f.label,
                              'evidence_status', f.evidence_status, 'valid_until', f.valid_until)), '[]')
                         FROM verdict_fact vf JOIN profile_fact f ON f.id = vf.fact_id WHERE vf.verdict_id = v.id) AS facts,
                      (SELECT q.id FROM question q WHERE q.verdict_id = v.id LIMIT 1) AS question_id
               FROM verdict v JOIN requirement r ON r.id = v.requirement_id
               LEFT JOIN passage p ON p.id = r.passage_id LEFT JOIN document d ON d.id = p.document_id
               WHERE v.assessment_id = %s ORDER BY (r.obligation = 'mandatory') DESC, v.id""", (a["id"],)).fetchall()
        decision = conn.execute("SELECT decision, decided_by, created_at FROM lot_decision WHERE lot_id = %s AND profile_id = %s",
                                (lot_id, profile_id)).fetchone()
    status = effective_status(lot["opp_status"], lot["status_reason"], lot["deadline"], lot["last_checked_at"],
                              datetime.now(timezone.utc))
    return {
        "assessment_id": a["id"], "lot": {"id": lot_id, "lot_no": lot["lot_no"], "title": lot["title"],
                                          "opportunity_id": lot["opportunity_id"], "opportunity": lot["opportunity_title"],
                                          "buyer": lot["buyer"], "deadline": lot["deadline"]},
        "status": {"status": status.status, "reason": status.reason, "last_checked_at": lot["last_checked_at"]},
        "version_id": a["version_id"], "on_current_version": a["version_id"] == lot["current_version_id"],
        "recommendation": a["recommendation"], "dimensions": a["dimensions"], "coverage": a["coverage"],
        "provisional": a["provisional"], "mode": a["mode"], "cost_usd": float(a["cost_usd"]),
        "budget_reached": a["budget_reached"], "created_at": a["created_at"], "decision": decision,
        "requirements": verdicts,
    }


class AssessIn(BaseModel):
    profile_id: int
    wait: bool = False


@router.post("/lots/{lot_id}/assess")
def assess(lot_id: int, body: AssessIn):
    with connect() as conn:
        _lot(conn, lot_id)
        if body.wait:
            from tenderfit.assessment.pipeline import assess_lot
            from tenderfit.llm import default_llm
            return {"assessment_id": assess_lot(conn, lot_id, body.profile_id, default_llm(), reason="requested")}
        return {"jobs": enqueue_assess(conn, [(lot_id, body.profile_id)], "requested")}


@router.get("/lots/{lot_id}/documents")
def lot_documents(lot_id: int):
    with connect() as conn:
        lot = _lot(conn, lot_id)
        docs.ensure_inventory(conn, lot["current_version_id"])
        conn.commit()
        return docs.inventory(conn, lot["current_version_id"])


@router.post("/lots/{lot_id}/documents")
async def upload_document(lot_id: int, file: UploadFile = File(...), ref_id: int | None = Form(None),
                          user: str = Form("user")):
    content = await file.read()
    with connect() as conn:
        lot = _lot(conn, lot_id)
        out = docs.attach_upload(conn, lot["current_version_id"], file.filename or "upload", content, user, ref_id)
        pairs = [(r["lot_id"], r["profile_id"]) for r in conn.execute(
            "SELECT DISTINCT lot_id, profile_id FROM assessment WHERE version_id = %s AND NOT superseded",
            (lot["current_version_id"],))]
        out["jobs"] = enqueue_assess(conn, pairs, "document uploaded: full reassessment")
    return out


@router.get("/profile/facts")
def get_facts(profile_id: int):
    with connect() as conn:
        return profiles.facts(conn, profile_id)


class FactUpdate(BaseModel):
    value: dict | None = None
    evidence_status: str | None = None
    valid_until: date | None = None
    confirmed_at: date | None = None
    confirm_by: date | None = None
    label: str | None = None


@router.put("/profile/facts/{fact_id}")
def put_fact(fact_id: int, body: FactUpdate):
    with connect() as conn:
        verdict_ids = profiles.update_fact(conn, fact_id, **body.model_dump(exclude_none=True))
        pairs = [(r["lot_id"], r["profile_id"]) for r in conn.execute(
            "SELECT DISTINCT a.lot_id, a.profile_id FROM verdict v JOIN assessment a ON a.id = v.assessment_id WHERE v.id = ANY(%s)",
            (verdict_ids,))]
        conn.commit()
        return {"verdicts_rechecked": verdict_ids, "jobs": enqueue_profile_change(conn, pairs, [fact_id], "profile fact changed")}


@router.post("/profile/evidence")
async def post_evidence(profile_id: int = Form(...), kind: str = Form(...), file: UploadFile = File(...),
                        fact_id: int | None = Form(None), page: int = Form(1), quote: str | None = Form(None),
                        expires_on: date | None = Form(None), issuer: str | None = Form(None)):
    content = await file.read()
    with connect() as conn:
        eid = profiles.add_evidence(conn, profile_id, kind, file.filename or "evidence", content, issuer, None, expires_on)
        verified = profiles.link_support(conn, fact_id, eid, page, quote) if fact_id and quote else None
        pairs = []
        if verified:
            pairs = [(r["lot_id"], r["profile_id"]) for r in conn.execute(
                "SELECT DISTINCT a.lot_id, a.profile_id FROM verdict v JOIN assessment a ON a.id = v.assessment_id "
                "WHERE v.id = ANY(%s)", (profiles.verdicts_using(conn, [fact_id]),))]
        conn.commit()
        return {"evidence_file_id": eid, "support_verified": verified,
                "jobs": enqueue_profile_change(conn, pairs, [fact_id], "evidence added") if pairs else []}


class AnswerIn(BaseModel):
    answer: str
    user: str = "user"


@router.post("/questions/{question_id}/answer")
def answer(question_id: int, body: AnswerIn):
    with connect() as conn:
        q = conn.execute("""UPDATE question SET answer = %s, answered_by = %s, answered_at = now() WHERE id = %s
                            RETURNING verdict_id""", (body.answer, body.user, question_id)).fetchone()
        if not q:
            raise HTTPException(404, "question not found")
        a = conn.execute("SELECT a.lot_id, a.profile_id FROM verdict v JOIN assessment a ON a.id = v.assessment_id "
                         "WHERE v.id = %s", (q["verdict_id"],)).fetchone()
        conn.commit()
        return {"jobs": enqueue_assess(conn, [(a["lot_id"], a["profile_id"])], "question answered")}


class ConfirmIn(BaseModel):
    point: str
    target_id: int
    decision: str
    user: str = "user"
    reason: str | None = None
    profile_id: int | None = None
    payload: dict | None = None


@router.post("/confirmations")
def post_confirmation(body: ConfirmIn):
    with connect() as conn:
        try:
            out = confirm(conn, body.point, body.user, body.target_id, body.decision, body.reason, body.profile_id,
                          body.payload)
        except ConfirmationError as e:
            raise HTTPException(422, str(e))
        pairs = out.pop("reassess")
        if body.point == "G4":
            out["jobs"] = enqueue_profile_change(conn, pairs, [body.target_id], f"G4 {body.decision}")
        else:
            out["jobs"] = enqueue_assess(conn, pairs, f"{body.point} {body.decision}")
        return out


@router.get("/assessments/{assessment_id}/trace")
def trace(assessment_id: int):
    with connect() as conn:
        a = conn.execute("SELECT * FROM assessment WHERE id = %s", (assessment_id,)).fetchone()
        if not a:
            raise HTTPException(404, "assessment not found")
        calls = conn.execute(
            """SELECT role, model, prompt_version, input_tokens, output_tokens, cost_usd, latency_ms, stop_reason, refs, created_at
               FROM llm_call WHERE (refs->>'version_id')::bigint = %s AND created_at <= coalesce(%s, now())
               ORDER BY id""", (a["version_id"], a["finished_at"])).fetchall()
        verdicts = conn.execute("""SELECT v.id, r.statement, v.decided_by, v.status, v.escalate, v.escalate_reason
                                   FROM verdict v JOIN requirement r ON r.id = v.requirement_id WHERE v.assessment_id = %s""",
                                (assessment_id,)).fetchall()
    return {"assessment": {k: a[k] for k in ("id", "mode", "full_reason", "cost_usd", "model_calls", "budget_reached",
                                             "created_at", "finished_at")},
            "model_calls": calls, "verdicts": verdicts}


@router.get("/jobs/{job_id}")
def job(job_id: int):
    with connect() as conn:
        j = conn.execute("SELECT id, kind, payload, status, attempts, last_error, created_at, finished_at FROM job WHERE id = %s",
                         (job_id,)).fetchone()
    if not j:
        raise HTTPException(404, "job not found")
    return j


@router.get("/changes")
def changes(profile_id: int | None = None, limit: int = 50):
    with connect() as conn:
        events = conn.execute(
            """SELECT c.*, o.title AS opportunity, b.name AS buyer, fv.published_at AS from_published, tv.published_at AS to_published,
                      fv.release_id AS from_release, tv.release_id AS to_release
               FROM change_event c JOIN opportunity o ON o.id = c.opportunity_id LEFT JOIN buyer b ON b.id = o.buyer_id
               LEFT JOIN notice_version fv ON fv.id = c.from_version_id LEFT JOIN notice_version tv ON tv.id = c.to_version_id
               ORDER BY c.detected_at DESC LIMIT %s""", (limit,)).fetchall()
        impacts = conn.execute(
            """SELECT i.*, l.lot_no, l.title AS lot_title, ab.recommendation AS before_recommendation,
                      aa.recommendation AS after_recommendation
               FROM change_impact i JOIN lot l ON l.id = i.lot_id
               LEFT JOIN assessment ab ON ab.id = i.before_assessment_id LEFT JOIN assessment aa ON aa.id = i.after_assessment_id
               WHERE i.change_id = ANY(%s) AND (%s::bigint IS NULL OR i.profile_id = %s) ORDER BY i.id""",
            ([e["id"] for e in events], profile_id, profile_id)).fetchall()
    by_change: dict[int, list] = {}
    for i in impacts:
        by_change.setdefault(i["change_id"], []).append(i)
    return [{**e, "impacts": by_change.get(e["id"], [])} for e in events
            if profile_id is None or e["kind"] == "status" or by_change.get(e["id"])]
