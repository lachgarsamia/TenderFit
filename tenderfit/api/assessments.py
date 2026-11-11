from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from tenderfit.api.deps import Identity, current_profile, current_user
from tenderfit.assessment.confirm import ConfirmationError, ConfirmationNotFound, confirm
from tenderfit.config import settings
from tenderfit.db import connect, must
from tenderfit.documents import service as docs
from tenderfit.jobs import queue
from tenderfit.profile import service as profiles

router = APIRouter(dependencies=[Depends(current_user)])


def enqueue_assess(conn, pairs: list[tuple[int, int]], reason: str) -> list[int]:
    ids = [queue.enqueue_unique(conn, "assess", {"lot_id": lot, "profile_id": prof, "reason": reason}, ("lot_id", "profile_id"),
                                lock_key=f"lot:{lot}") for lot, prof in sorted(set(pairs))]
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


def _read_upload(file: UploadFile, allowed: tuple[str, ...]) -> bytes:
    """Bounded read and a content check: the extension alone is not trusted."""
    name = (file.filename or "").lower()
    if not name.endswith(allowed):
        raise HTTPException(415, f"only {', '.join(allowed)} files are accepted")
    content = file.file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, f"file is larger than {settings.max_upload_bytes // (1024 * 1024)} MB")
    if (name.endswith(".pdf") and content[:5] != b"%PDF-") or (name.endswith(".docx") and content[:2] != b"PK"):
        raise HTTPException(415, "the file's contents do not match its extension")
    return content


def _lot_out(lot_id: int, lot: dict) -> dict:
    return {"id": lot_id, "lot_no": lot["lot_no"], "title": lot["title"], "opportunity_id": lot["opportunity_id"],
            "opportunity": lot["opportunity_title"], "buyer": lot["buyer"], "deadline": lot["deadline"]}


def _current_assessment(conn, lot_id: int, profile_id: int) -> dict | None:
    return conn.execute("""SELECT * FROM assessment WHERE lot_id = %s AND profile_id = %s AND status = 'done'
                           AND NOT superseded AND NOT shadow ORDER BY id DESC LIMIT 1""", (lot_id, profile_id)).fetchone()


def _processing_state(conn, lot_id: int, profile_id: int, current_id: int | None) -> tuple[dict | None, dict | None]:
    """What is happening now for this lot and profile, and the newest failed attempt since the current assessment."""
    run = conn.execute("""SELECT id, job_id, started_at, progress FROM assessment WHERE lot_id = %s AND profile_id = %s
                          AND status = 'running' AND NOT shadow ORDER BY id DESC LIMIT 1""", (lot_id, profile_id)).fetchone()
    job = conn.execute(
        """SELECT id, kind, status, attempts, max_attempts, created_at, run_after, last_error FROM job
           WHERE kind IN ('assess', 'profile_change') AND status IN ('queued', 'running')
             AND (payload->>'lot_id')::bigint = %s AND (payload->>'profile_id')::bigint = %s
           ORDER BY id DESC LIMIT 1""", (lot_id, profile_id)).fetchone()
    processing = None
    if run or job:
        progress = (run or {}).get("progress") or {}
        processing = {"status": "running" if run else job["status"], "job_id": (run or {}).get("job_id") or (job or {}).get("id"),
                      "started_at": run["started_at"] if run else None, "queued_at": job["created_at"] if job else None,
                      "stage": progress.get("stage"), "done": progress.get("done"), "total": progress.get("total"),
                      "attempt": job["attempts"] if job else None, "max_attempts": job["max_attempts"] if job else None,
                      "retrying": bool(job and job["status"] == "queued" and job["attempts"] > 0)}
    failed = conn.execute(
        """SELECT id, error, error_stage, finished_at FROM assessment WHERE lot_id = %s AND profile_id = %s AND status = 'failed'
           AND NOT shadow AND id > %s ORDER BY id DESC LIMIT 1""", (lot_id, profile_id, current_id or 0)).fetchone()
    last_failure = {"assessment_id": failed["id"], "error": failed["error"], "stage": failed["error_stage"],
                    "at": failed["finished_at"]} if failed else None
    return processing, last_failure


@router.get("/lots/{lot_id}/assessment")
def read_assessment(lot_id: int, profile_id: int = Depends(current_profile)):
    return get_assessment(lot_id, profile_id)


def get_assessment(lot_id: int, profile_id: int) -> dict:
    """profile_id comes from the session (see api/deps.py), never from the request."""
    from tenderfit.api.main import effective_status

    with connect() as conn:
        lot = _lot(conn, lot_id)
        a = _current_assessment(conn, lot_id, profile_id)
        processing, last_failure = _processing_state(conn, lot_id, profile_id, a["id"] if a else None)
        if not a:
            if processing or last_failure:
                return {"assessment_id": None, "requirements": [], "processing": processing, "last_failure": last_failure,
                        "lot": _lot_out(lot_id, lot), "version": None, "status": None}
            raise HTTPException(404, "no assessment yet; POST /lots/{id}/assess")
        verdicts = conn.execute(
            """SELECT v.id, v.status, v.reason, v.basis, v.needs_human, v.provisional, v.question, v.escalate,
                      v.escalate_reason, v.decided_by, v.override_status, v.override_reason, v.override_by, v.carried_over,
                      v.required, v.observed, v.evidence_quality, v.uncertainty, v.retrieval, v.reference_gaps,
                      r.condition_note, r.scope, r.id AS requirement_id, r.statement, r.category, r.obligation, r.quote, r.section, r.page,
                      r.equivalent_allowed, coalesce(d.filename, 'document') AS document,
                      (SELECT coalesce(jsonb_agg(jsonb_build_object('passage_id', c.passage_id, 'quote', c.quote,
                              'section', p2.section, 'page', p2.page, 'document', coalesce(d2.filename, 'document'), 'document_id', d2.id)), '[]')
                         FROM verdict_citation c JOIN passage p2 ON p2.id = c.passage_id JOIN document d2 ON d2.id = p2.document_id
                         WHERE c.verdict_id = v.id) AS citations,
                      (SELECT coalesce(jsonb_agg(jsonb_build_object('id', f.id, 'label', f.label,
                              'evidence_status', f.evidence_status, 'valid_until', f.valid_until,
                              'supports', (SELECT coalesce(jsonb_agg(jsonb_build_object('filename', e.filename, 'page', s.page, 'quote', s.quote,
                                                      'human_verified_by', s.human_verified_by)), '[]')
                                           FROM fact_support s JOIN evidence_file e ON e.id = s.evidence_file_id
                                           WHERE s.fact_id = f.id AND s.verified),
                              'verification_level', (SELECT fv.level FROM fact_verification fv WHERE fv.fact_id = f.id ORDER BY fv.id DESC LIMIT 1))), '[]')
                         FROM verdict_fact vf JOIN profile_fact f ON f.id = vf.fact_id WHERE vf.verdict_id = v.id) AS facts,
                      (SELECT coalesce(jsonb_agg(jsonb_build_object('label', rc.label, 'mandatory', rc.mandatory, 'status', vc.status,
                              'reason', vc.reason, 'decided_by', vc.decided_by, 'required', vc.required, 'observed', vc.observed,
                              'quote', rc.quote, 'note', rc.condition_note) ORDER BY rc.ordinal), '[]')
                         FROM verdict_condition vc JOIN requirement_condition rc ON rc.id = vc.condition_id WHERE vc.verdict_id = v.id) AS conditions,
                      (SELECT q.id FROM question q WHERE q.verdict_id = v.id ORDER BY q.id LIMIT 1) AS question_id,
                      (SELECT q.answer FROM question q WHERE q.verdict_id = v.id ORDER BY q.id LIMIT 1) AS answer
               FROM verdict v JOIN requirement r ON r.id = v.requirement_id
               LEFT JOIN passage p ON p.id = r.passage_id LEFT JOIN document d ON d.id = p.document_id
               WHERE v.assessment_id = %s ORDER BY (r.obligation = 'mandatory') DESC, v.id""", (a["id"],)).fetchall()
        decision = conn.execute("SELECT decision, decided_by, created_at FROM lot_decision WHERE lot_id = %s AND profile_id = %s",
                                (lot_id, profile_id)).fetchone()
        steps: dict[int, list] = {}
        for st in conn.execute("""SELECT requirement_id, tool, input FROM agent_step WHERE assessment_id = %s
                                  ORDER BY requirement_id, step""", (a["id"],)):
            steps.setdefault(st["requirement_id"], []).append({"tool": st["tool"], "input": st["input"]})
        ver = conn.execute(
            """SELECT nv.release_id, nv.source_url, nv.notice_type, nv.published_at, nvn.version_no,
                      (SELECT count(*) FROM notice_version x WHERE x.ocid = nv.ocid) AS versions
               FROM notice_version nv JOIN notice_version_numbered nvn ON nvn.id = nv.id WHERE nv.id = %s""",
            (a["version_id"],)).fetchone()
        profile = conn.execute("SELECT name, is_fictional FROM business_profile WHERE id = %s", (profile_id,)).fetchone()
        rejections = conn.execute(
            """SELECT rj.statement, rj.reason, rj.kind, rj.obligation, rj.lot_no FROM requirement_rejection rj
               LEFT JOIN passage p ON p.id = rj.passage_id LEFT JOIN document d ON d.id = p.document_id
               WHERE rj.version_id = %s AND (d.owner_profile_id IS NULL OR d.owner_profile_id = %s)
                 AND (rj.lot_no IS NULL OR rj.lot_no = %s OR rj.kind = 'lot') ORDER BY rj.id""",
            (a["version_id"], profile_id, lot["lot_no"])).fetchall()
        siblings = conn.execute(
            """SELECT l.id, l.lot_no, l.title, l.status,
                      (SELECT recommendation FROM assessment x WHERE x.lot_id = l.id AND x.profile_id = %s AND x.status = 'done'
                       AND NOT x.superseded AND NOT x.shadow LIMIT 1) AS recommendation
               FROM lot l WHERE l.opportunity_id = %s ORDER BY l.lot_no""", (profile_id, lot["opportunity_id"])).fetchall()
        fictional = must(conn.execute("SELECT ocid LIKE 'ocds-test-%%' f FROM opportunity WHERE id = %s",
                                 (lot["opportunity_id"],)).fetchone())["f"]
    status = effective_status(lot["opp_status"], lot["status_reason"], lot["deadline"], lot["last_checked_at"],
                              datetime.now(timezone.utc))
    return {
        "assessment_id": a["id"], "lot": _lot_out(lot_id, lot), "processing": processing, "last_failure": last_failure,
        "status": {"status": status.status, "reason": status.reason, "last_checked_at": lot["last_checked_at"]},
        "version_id": a["version_id"], "on_current_version": a["version_id"] == lot["current_version_id"],
        "recommendation": a["recommendation"], "dimensions": a["dimensions"], "coverage": a["coverage"],
        "provisional": a["provisional"], "mode": a["mode"], "cost_usd": float(a["cost_usd"]),
        "budget_reached": a["budget_reached"], "created_at": a["created_at"], "decision": decision,
        "version": ver, "profile": profile, "fictional_notice": fictional, "rejections": rejections, "lots": siblings,
        "requirements": [{**v, "agent_steps": steps.get(v["requirement_id"], [])} for v in verdicts],
    }


@router.post("/lots/{lot_id}/assess")
def assess(lot_id: int, profile_id: int = Depends(current_profile)):
    with connect() as conn:
        _lot(conn, lot_id)
        return {"jobs": enqueue_assess(conn, [(lot_id, profile_id)], "requested")}


@router.get("/lots/{lot_id}/documents")
def lot_documents(lot_id: int, profile_id: int = Depends(current_profile)):
    with connect() as conn:
        lot = _lot(conn, lot_id)
        docs.ensure_inventory(conn, lot["current_version_id"])
        conn.commit()
        return docs.inventory(conn, lot["current_version_id"], profile_id)


@router.post("/lots/{lot_id}/documents")
def upload_document(lot_id: int, file: UploadFile = File(...), ref_id: int | None = Form(None),
                    profile_id: int = Depends(current_profile), me: Identity = Depends(current_user)):
    """A document a supplier uploads is private to that supplier: it changes no one else's coverage or requirements."""
    content = _read_upload(file, (".pdf", ".docx"))
    with connect() as conn:
        lot = _lot(conn, lot_id)
        try:
            out = docs.attach_upload(conn, lot["current_version_id"], file.filename or "upload", content, me.email, ref_id, profile_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        pairs = [(r["lot_id"], r["profile_id"]) for r in conn.execute(
            """SELECT DISTINCT lot_id, profile_id FROM assessment WHERE version_id = %s AND profile_id = %s AND status = 'done'
               AND NOT superseded AND NOT shadow""", (lot["current_version_id"], profile_id))]
        out["jobs"] = enqueue_assess(conn, pairs, "document uploaded: full reassessment")
    return out


@router.get("/profile/facts")
def get_facts(profile_id: int = Depends(current_profile)):
    with connect() as conn:
        return profiles.facts(conn, profile_id)


def _own_fact(conn, fact_id: int, profile_id: int) -> dict:
    fact = profiles.owned_fact(conn, fact_id, profile_id)
    if not fact:
        raise HTTPException(404, "fact not found")
    return fact


def _lots_using(conn, verdict_ids: list[int], profile_id: int) -> list[tuple[int, int]]:
    return [(r["lot_id"], r["profile_id"]) for r in conn.execute(
        """SELECT DISTINCT a.lot_id, a.profile_id FROM verdict v JOIN assessment a ON a.id = v.assessment_id
           WHERE v.id = ANY(%s) AND a.profile_id = %s""", (verdict_ids, profile_id))]


class FactIn(BaseModel):
    attribute: str = Field(max_length=40)
    label: str = Field(max_length=300)
    value: dict = Field(default_factory=dict)
    evidence_status: Literal["declared", "missing", "unknown"] = "declared"
    valid_until: date | None = None


@router.post("/profile/facts", status_code=201)
def post_fact(body: FactIn, profile_id: int = Depends(current_profile)):
    """A new fact can settle requirements that nothing could before, so every current assessment is re-checked."""
    with connect() as conn:
        try:
            fid = profiles.add_declared_fact(conn, profile_id, body.attribute, body.label, body.value, body.evidence_status, body.valid_until)
        except profiles.ProfileError as e:
            raise HTTPException(422, str(e))
        pairs = [(r["lot_id"], profile_id) for r in conn.execute(
            """SELECT DISTINCT lot_id FROM assessment WHERE profile_id = %s AND status = 'done' AND NOT superseded AND NOT shadow""", (profile_id,))]
        conn.commit()
        return {"id": fid, "jobs": enqueue_assess(conn, pairs, "new fact added")}


class FactUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=300)
    value: dict | None = None
    evidence_status: Literal["declared", "missing", "unknown"] | None = None
    valid_until: date | None = None
    clear_expiry: bool = False


@router.put("/profile/facts/{fact_id}")
def put_fact(fact_id: int, body: FactUpdate, profile_id: int = Depends(current_profile)):
    with connect() as conn:
        _own_fact(conn, fact_id, profile_id)
        try:
            out = profiles.edit_fact(conn, fact_id, profile_id, body.label, body.value, body.evidence_status, body.valid_until, body.clear_expiry)
        except profiles.ProfileError as e:
            raise HTTPException(422, str(e))
        pairs = _lots_using(conn, out["verdicts"], profile_id)
        conn.commit()
        return {"detached_from_documents": out["detached_from_documents"], "verdicts_rechecked": out["verdicts"],
                "jobs": enqueue_profile_change(conn, pairs, [fact_id], "profile fact changed")}


@router.post("/profile/evidence")
def post_evidence(kind: str = Form(..., max_length=40), file: UploadFile = File(...),
                  fact_id: int | None = Form(None), page: int = Form(1, ge=1), quote: str | None = Form(None),
                  expires_on: date | None = Form(None), issuer: str | None = Form(None), profile_id: int = Depends(current_profile)):
    content = _read_upload(file, (".pdf", ".docx", ".txt"))
    with connect() as conn:
        if fact_id:
            _own_fact(conn, fact_id, profile_id)
        try:
            eid = profiles.add_evidence(conn, profile_id, kind, file.filename or "evidence", content, issuer, None, expires_on)
        except profiles.ProfileError as e:
            raise HTTPException(422, str(e))
        verified = profiles.link_support(conn, fact_id, eid, page, quote) if fact_id and quote else None
        conn.commit()
        jobs: list[int] = []
        if verified and fact_id:
            jobs = enqueue_profile_change(conn, _lots_using(conn, profiles.verdicts_using(conn, [fact_id]), profile_id), [fact_id], "evidence added")
        return {"evidence_file_id": eid, "support_verified": verified, "jobs": jobs}


@router.post("/profile/facts/{fact_id}/renew")
def renew_fact(fact_id: int, file: UploadFile = File(...), quote: str = Form(...), page: int = Form(1, ge=1),
               expires_on: date = Form(...), issuer: str | None = Form(None), profile_id: int = Depends(current_profile)):
    if expires_on < date.today():
        raise HTTPException(422, "the renewed document must expire in the future")
    content = _read_upload(file, (".pdf", ".docx", ".txt"))
    with connect() as conn:
        _own_fact(conn, fact_id, profile_id)
        try:
            out = profiles.renew_evidence(conn, fact_id, file.filename or "renewal", content, page, quote, expires_on, issuer)
        except profiles.ProfileError as e:
            raise HTTPException(422, str(e))
        checked = conn.execute("SELECT level, checks FROM fact_verification WHERE fact_id = %s ORDER BY id DESC LIMIT 1", (fact_id,)).fetchone()
        conn.commit()
        pairs = _lots_using(conn, out["verdicts"], profile_id) if out["verified"] else []
        return {"verified": out["verified"], "verification": checked,
                "jobs": enqueue_profile_change(conn, pairs, [fact_id], "evidence renewed") if pairs else []}


@router.post("/profile/facts/{fact_id}/verify")
def verify_fact(fact_id: int, profile_id: int = Depends(current_profile), me: Identity = Depends(current_user)):
    """The name recorded is the signed-in person's, not something the request says."""
    with connect() as conn:
        _own_fact(conn, fact_id, profile_id)
        if not profiles.human_verify(conn, fact_id, me.email):
            raise HTTPException(409, "no checked document is linked to this fact")
    return {"verified_by": me.email}


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


@router.post("/questions/{question_id}/answer")
def answer(question_id: int, body: AnswerIn, profile_id: int = Depends(current_profile), me: Identity = Depends(current_user)):
    with connect() as conn:
        q = conn.execute("""UPDATE question SET answer = %s, answered_by = %s, answered_at = now() WHERE id = %s AND profile_id = %s
                            RETURNING verdict_id""", (body.answer, me.email, question_id, profile_id)).fetchone()
        if not q:
            raise HTTPException(404, "question not found")
        a = must(conn.execute("SELECT a.lot_id, a.profile_id FROM verdict v JOIN assessment a ON a.id = v.assessment_id "
                         "WHERE v.id = %s", (q["verdict_id"],)).fetchone())
        conn.commit()
        return {"jobs": enqueue_assess(conn, [(a["lot_id"], a["profile_id"])], "question answered")}


class ConfirmIn(BaseModel):
    point: str
    target_id: int
    decision: str
    reason: str | None = Field(default=None, max_length=4000)
    payload: dict | None = None


@router.post("/confirmations")
def post_confirmation(body: ConfirmIn, profile_id: int = Depends(current_profile), me: Identity = Depends(current_user)):
    with connect() as conn:
        try:
            out = confirm(conn, body.point, me.email, body.target_id, body.decision, body.reason, profile_id, body.payload)
        except ConfirmationNotFound:
            raise HTTPException(404, "not found")
        except ConfirmationError as e:
            raise HTTPException(422, str(e))
        pairs = [p for p in out.pop("reassess") if p[1] == profile_id]
        if body.point == "G4":
            out["jobs"] = enqueue_profile_change(conn, pairs, [body.target_id], f"G4 {body.decision}")
        else:
            out["jobs"] = enqueue_assess(conn, pairs, f"{body.point} {body.decision}")
        return out


@router.get("/assessments/{assessment_id}/trace")
def trace(assessment_id: int, profile_id: int = Depends(current_profile)):
    with connect() as conn:
        a = conn.execute("SELECT * FROM assessment WHERE id = %s AND profile_id = %s", (assessment_id, profile_id)).fetchone()
        if not a:
            raise HTTPException(404, "assessment not found")
        calls = conn.execute(
            """SELECT role, model, prompt_version, input_tokens, output_tokens, cost_usd, latency_ms, stop_reason, refs, created_at
               FROM llm_call WHERE (refs->>'version_id')::bigint = %s AND created_at <= coalesce(%s, now())
                 AND refs->>'profile_id' = %s::text
               ORDER BY id""", (a["version_id"], a["finished_at"], profile_id)).fetchall()
        verdicts = conn.execute("""SELECT v.id, r.statement, v.decided_by, v.status, v.escalate, v.escalate_reason
                                   FROM verdict v JOIN requirement r ON r.id = v.requirement_id WHERE v.assessment_id = %s""",
                                (assessment_id,)).fetchall()
    return {"assessment": {k: a[k] for k in ("id", "mode", "full_reason", "cost_usd", "model_calls", "budget_reached",
                                             "created_at", "finished_at")},
            "model_calls": calls, "verdicts": verdicts}


@router.get("/jobs/{job_id}")
def job(job_id: int, profile_id: int = Depends(current_profile)):
    """Only this supplier's own jobs. Shared jobs such as imports are not visible, and errors are cut to their last line."""
    with connect() as conn:
        j = conn.execute("""SELECT id, kind, status, attempts, last_error, created_at, finished_at FROM job
                            WHERE id = %s AND (payload->>'profile_id')::bigint = %s""", (job_id, profile_id)).fetchone()
    if not j:
        raise HTTPException(404, "job not found")
    lines = (j["last_error"] or "").strip().splitlines()
    return {**j, "last_error": lines[-1][:500] if lines else None}


def _snippet(old: str, new: str, context: int = 4) -> tuple[str, str]:
    import difflib
    a, b = old.split(), new.split()
    ops = [o for o in difflib.SequenceMatcher(None, a, b).get_opcodes() if o[0] != "equal"]
    if not ops:
        return "", ""
    i1, j1 = max(ops[0][1] - context, 0), max(ops[0][3] - context, 0)
    i2, j2 = min(ops[-1][2] + context, len(a)), min(ops[-1][4] + context, len(b))
    return (("… " if i1 else "") + " ".join(a[i1:i2]) + (" …" if i2 < len(a) else ""),
            ("… " if j1 else "") + " ".join(b[j1:j2]) + (" …" if j2 < len(b) else ""))


@router.post("/changes/{change_id}/retry")
def retry_change(change_id: int, profile_id: int = Depends(current_profile)):
    from tenderfit.changes.apply import retry_event
    with connect() as conn:
        mine = conn.execute("""SELECT 1 FROM change_event c JOIN assessment a ON a.version_id = c.from_version_id
                               WHERE c.id = %s AND a.profile_id = %s AND a.status = 'done' LIMIT 1""", (change_id, profile_id)).fetchone()
        if not mine:
            raise HTTPException(404, "change not found")
        if not retry_event(conn, change_id):
            raise HTTPException(409, "only a failed amendment can be retried")
    return {"status": "detected"}


@router.get("/changes")
def changes(limit: int = Query(50, ge=1, le=200), profile_id: int = Depends(current_profile)):
    with connect() as conn:
        # Scope first and limit afterwards, so other suppliers' events never use up this supplier's page.
        # A version change stays listed for anyone who assessed the old version, even with no impact: a re-check can
        # reach the new version before the change is processed, and the amendment must not then vanish.
        events = conn.execute(
            """SELECT c.*, o.title AS opportunity, b.name AS buyer, fv.published_at AS from_published, tv.published_at AS to_published,
                      fv.release_id AS from_release, tv.release_id AS to_release,
                      fn.version_no AS from_version_no, tn.version_no AS to_version_no
               FROM change_event c JOIN opportunity o ON o.id = c.opportunity_id LEFT JOIN buyer b ON b.id = o.buyer_id
               LEFT JOIN notice_version fv ON fv.id = c.from_version_id LEFT JOIN notice_version tv ON tv.id = c.to_version_id
               LEFT JOIN notice_version_numbered fn ON fn.id = c.from_version_id
               LEFT JOIN notice_version_numbered tn ON tn.id = c.to_version_id
               WHERE (c.kind = 'profile' AND (c.summary->>'profile_id')::bigint = %(p)s)
                  OR EXISTS (SELECT 1 FROM change_impact i WHERE i.change_id = c.id AND i.profile_id = %(p)s)
                  OR (c.kind = 'version' AND EXISTS (
                        SELECT 1 FROM assessment a WHERE a.version_id = c.from_version_id AND a.profile_id = %(p)s AND a.status = 'done'))
                  OR (c.kind = 'status' AND EXISTS (
                        SELECT 1 FROM assessment a JOIN lot l ON l.id = a.lot_id
                        WHERE l.opportunity_id = c.opportunity_id AND a.profile_id = %(p)s AND a.status = 'done'))
               ORDER BY c.detected_at DESC, c.id DESC LIMIT %(l)s""", {"p": profile_id, "l": limit}).fetchall()
        impacts = conn.execute(
            """SELECT i.*, l.lot_no, l.title AS lot_title, ab.recommendation AS before_recommendation,
                      aa.recommendation AS after_recommendation
               FROM change_impact i JOIN lot l ON l.id = i.lot_id
               LEFT JOIN assessment ab ON ab.id = i.before_assessment_id LEFT JOIN assessment aa ON aa.id = i.after_assessment_id
               WHERE i.change_id = ANY(%s) AND i.profile_id = %s ORDER BY l.lot_no, i.id""",
            ([e["id"] for e in events], profile_id)).fetchall()
        ids = {x for e in events for p in e["passages"] for x in (p.get("old_id"), p.get("new_id")) if x}
        texts = {r["id"]: r["text"] for r in conn.execute("SELECT id, text FROM passage WHERE id = ANY(%s)", (list(ids),))}
    for e in events:
        for p in e["passages"]:
            before, after = texts.get(p.get("old_id"), ""), texts.get(p.get("new_id"), "")
            p["before"], p["after"] = _snippet(before, after) if p["kind"] == "modified" else (before[:160], after[:160])
    by_change: dict[int, list] = {}
    for i in impacts:
        by_change.setdefault(i["change_id"], []).append(i)
    return [{**e, "impacts": by_change.get(e["id"], [])} for e in events]
