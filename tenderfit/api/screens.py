import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from tenderfit.api.deps import current_profile, current_user
from tenderfit.config import settings
from tenderfit.db import connect
from tenderfit.documents.service import inventory
from tenderfit.rules import aggregate as agg

router = APIRouter(dependencies=[Depends(current_user)])

REGIONS = {"north east": "UKC", "north west": "UKD", "yorkshire": "UKE", "humber": "UKE", "east midlands": "UKF",
           "west midlands": "UKG", "east of england": "UKH", "london": "UKI", "south east": "UKJ", "south west": "UKK",
           "wales": "UKL", "scotland": "UKM", "northern ireland": "UKN"}
SECTORS = {"furniture": "391", "desk": "3916", "chair": "3911", "seating": "3911", "storage": "3912", "shelving": "3912",
           "furnishing": "39"}


def parse_request(q: str) -> dict:
    low = q.lower()
    regions = sorted({code for name, code in REGIONS.items() if name in low})
    cpv = sorted({code for word, code in SECTORS.items() if re.search(rf"\b{word}", low)})
    words = [w for w in re.findall(r"[a-z]{4,}", low) if w not in {"tenders", "tender", "could", "deliver", "which",
                                                                      "deserve", "closer", "look", "find", "that",
                                                                      "with", "from", "into", "show", "open", "tell", "deserve"}
             and not any(w in n for n in REGIONS) and not any(w.startswith(s) for s in SECTORS)]
    return {"regions": regions, "cpv": cpv, "terms": words[:6]}


def _fit(conn, lot_ids: list[int], profile_id: int, profile: dict) -> dict[int, dict]:
    rows = {r["lot_id"]: r for r in conn.execute(
        """SELECT DISTINCT ON (lot_id) lot_id, recommendation, dimensions, coverage, provisional, version_id, created_at
           FROM assessment WHERE lot_id = ANY(%s) AND profile_id = %s AND status = 'done' AND NOT superseded AND NOT shadow
           ORDER BY lot_id, id DESC""", (lot_ids, profile_id))}
    lots = {r["id"]: r for r in conn.execute(
        """SELECT l.id, coalesce(nv.fields, '{}'::jsonb) AS f, l.lot_no, o.cpv_codes, o.regions FROM lot l
           JOIN opportunity o ON o.id = l.opportunity_id LEFT JOIN notice_version nv ON nv.id = o.current_version_id
           WHERE l.id = ANY(%s)""", (lot_ids,))}
    out = {}
    for lid in lot_ids:
        a = rows.get(lid)
        if a:
            el = (a["dimensions"] or {}).get("eligibility", {})
            kind = {"blocker_found": "blocker", "provisional": "provisional", "not_assessed": "provisional"}.get(
                el.get("state"), "relevant")
            if (a["dimensions"] or {}).get("relevance", {}).get("level") == "low":
                kind = "low"
            head, _, rest = (a["recommendation"] or "").partition(":")
            out[lid] = {"assessed": True, "kind": kind, "label": head, "detail": rest.strip(), "provisional": a["provisional"],
                        "coverage": a["coverage"]}
            continue
        lf = next((x for x in lots[lid]["f"].get("lots", []) if x["id"] == lots[lid]["lot_no"]), {})
        rel = agg.relevance(lf.get("cpv") or lots[lid]["cpv_codes"], profile["cpv"], lf.get("regions") or lots[lid]["regions"],
                            profile["regions"])
        out[lid] = {"assessed": False, "kind": "low" if rel["level"] == "low" else "unassessed",
                    "label": {"strong": "Likely relevant", "partial": "Possibly relevant", "low": "Low relevance"}[rel["level"]],
                    "detail": rel["detail"] + ("" if rel["level"] == "low" else " · not assessed yet"),
                    "provisional": None, "coverage": None}
    return out


def _profile(conn, profile_id: int) -> dict:
    p = conn.execute("SELECT * FROM business_profile WHERE id = %s", (profile_id,)).fetchone()
    if not p:
        raise HTTPException(404, "profile not found")
    cpv = [c for r in conn.execute("SELECT value FROM profile_fact WHERE profile_id = %s AND attribute = 'product'", (profile_id,))
           for c in r["value"].get("cpv", [])]
    return {**p, "cpv": cpv}


class SearchIn(BaseModel):
    q: str = Field(default="", max_length=500)
    cpv: list[str] | None = None
    regions: list[str] | None = None
    min_days: int = 0
    limit: int = Field(default=30, ge=1, le=200)


@router.post("/search")
def search(body: SearchIn, profile_id: int = Depends(current_profile)):
    from tenderfit.api.main import effective_status

    parsed = parse_request(body.q)
    now = datetime.now(timezone.utc)
    with connect() as conn:
        profile = _profile(conn, profile_id)
        # With nothing typed, the supplier's own products and regions are the search; no sector is assumed.
        from_profile = not body.q.strip()
        cpv = body.cpv if body.cpv is not None else (sorted({c[:3] for c in profile["cpv"]}) if from_profile else parsed["cpv"])
        regions = body.regions if body.regions is not None else (profile["regions"] if from_profile else parsed["regions"])
        where, args = ["TRUE"], []
        if cpv:
            where.append("EXISTS (SELECT 1 FROM unnest(o.cpv_codes) c WHERE c LIKE ANY(%s))")
            args.append([f"{c}%" for c in cpv])
        if regions:
            where.append("(o.regions = '{}' OR EXISTS (SELECT 1 FROM unnest(o.regions) r WHERE r LIKE ANY(%s)))")
            args.append([f"{r}%" for r in regions])
        if parsed["terms"]:
            where.append("to_tsvector('english', coalesce(o.title, '') || ' ' || coalesce(nv.fields->>'description', '')) "
                         "@@ to_tsquery('english', %s)")
            args.append(" | ".join(parsed["terms"]))
        select = f"""
            SELECT o.id, o.ocid, o.title, o.deadline, o.status, o.status_reason, o.last_checked_at, b.name AS buyer,
                   nv.release_id, nv.published_at, nv.notice_type,
                   (SELECT count(*) FROM notice_version v WHERE v.ocid = o.ocid) AS versions,
                   (SELECT max(v.published_at) FROM notice_version v WHERE v.ocid = o.ocid) AS last_version_at,
                   (SELECT jsonb_agg(jsonb_build_object('id', l.id, 'lot_no', l.lot_no, 'title', l.title, 'status', l.status)
                                     ORDER BY l.lot_no) FROM lot l WHERE l.opportunity_id = o.id) AS lots
            FROM opportunity o LEFT JOIN buyer b ON b.id = o.buyer_id LEFT JOIN notice_version nv ON nv.id = o.current_version_id
            WHERE {' AND '.join(where)}"""
        # Tenders that are still open fill the page. Closed ones are listed only if this supplier assessed them.
        rows = conn.execute(f"""{select} AND o.status NOT IN ('cancelled', 'awarded') AND (o.deadline IS NULL OR o.deadline > now())
                                ORDER BY o.deadline NULLS LAST, o.id LIMIT %s""", (*args, body.limit * 3)).fetchall()
        closed = conn.execute(
            f"""{select} AND (o.status IN ('cancelled', 'awarded') OR o.deadline <= now())
                AND EXISTS (SELECT 1 FROM assessment a JOIN lot l ON l.id = a.lot_id WHERE l.opportunity_id = o.id
                            AND a.profile_id = %s AND a.status = 'done')
                ORDER BY o.deadline DESC NULLS LAST, o.id LIMIT %s""", (*args, profile_id, body.limit)).fetchall()
        lot_ids = [l["id"] for r in rows for l in (r["lots"] or [])]
        fits = _fit(conn, lot_ids, profile_id, profile) if lot_ids else {}
        wm = conn.execute("SELECT watermark FROM import_state").fetchone()

    def item(r, s):
        return {**r, "status": s.status, "status_reason": s.reason, "fictional": r["ocid"].startswith("ocds-test-"),
                "lots": [{**l, "fit": fits.get(l["id"])} for l in (r["lots"] or [])]}

    shortlist, gone = [], []
    for r in rows:
        s = effective_status(r["status"], r["status_reason"], r["deadline"], r["last_checked_at"], now)
        if body.min_days and r["deadline"] and r["deadline"] < now + timedelta(days=body.min_days):
            continue
        shortlist.append(item(r, s))
    for r in closed:
        gone.append(item(r, effective_status(r["status"], r["status_reason"], r["deadline"], r["last_checked_at"], now)))
    order = {"relevant": 0, "provisional": 1, "blocker": 2, "unassessed": 3, "low": 4}
    shortlist.sort(key=lambda o: (min((order.get((l["fit"] or {}).get("kind"), 5) for l in o["lots"]), default=5),
                                  o["deadline"] or now + timedelta(days=999)))
    return {"parsed": {"regions": regions, "cpv": cpv, "terms": parsed["terms"], "from_profile": from_profile},
            "last_import": wm and wm["watermark"], "shortlist": shortlist[:body.limit], "no_longer_open": gone}


@router.get("/tenders/{opportunity_id}")
def tender(opportunity_id: int, profile_id: int = Depends(current_profile)):
    from tenderfit.api.main import effective_status

    with connect() as conn:
        profile = _profile(conn, profile_id)
        o = conn.execute(
            """SELECT o.*, b.name AS buyer, nv.fields, nv.source_url, nv.published_at, nv.release_id FROM opportunity o
               LEFT JOIN buyer b ON b.id = o.buyer_id LEFT JOIN notice_version nv ON nv.id = o.current_version_id WHERE o.id = %s""",
            (opportunity_id,)).fetchone()
        if not o:
            raise HTTPException(404, "tender not found")
        lots = conn.execute("SELECT id, lot_no, title, status, quantity FROM lot WHERE opportunity_id = %s ORDER BY lot_no", (opportunity_id,)).fetchall()
        fits = _fit(conn, [l["id"] for l in lots], profile_id, profile) if lots else {}
        versions = conn.execute(
            """SELECT id, version_no, release_id, release_tag, notice_type, published_at, source_url FROM notice_version_numbered
               WHERE opportunity_id = %s ORDER BY version_no""", (opportunity_id,)).fetchall()
        documents = inventory(conn, o["current_version_id"], profile_id)["documents"] if o["current_version_id"] else []
    fields = o["fields"] or {}
    by_lot = {str(l["id"]): l for l in fields.get("lots", [])}
    s = effective_status(o["status"], o["status_reason"], o["deadline"], o["last_checked_at"], datetime.now(timezone.utc))
    return {"id": o["id"], "ocid": o["ocid"], "title": o["title"], "buyer": o["buyer"], "deadline": o["deadline"], "description": fields.get("description"),
            "value": fields.get("value"), "regions": o["regions"], "cpv_codes": o["cpv_codes"], "legal_basis": o["legal_basis"],
            "procurement_method": fields.get("procurement_method"), "source_url": o["source_url"], "status": s.status, "status_reason": s.reason,
            "last_checked_at": o["last_checked_at"], "fictional": o["ocid"].startswith("ocds-test-"), "versions": versions, "documents": documents,
            "lots": [{**l, "description": by_lot.get(l["lot_no"], {}).get("description"), "value": by_lot.get(l["lot_no"], {}).get("value"),
                      "contract_period": by_lot.get(l["lot_no"], {}).get("contract_period"), "fit": fits.get(l["id"])} for l in lots]}


@router.get("/lots/{lot_id}")
def lot_summary(lot_id: int):
    with connect() as conn:
        lot = conn.execute(
            """SELECT l.id, l.lot_no, l.title, l.opportunity_id, o.title AS opportunity, o.deadline, b.name AS buyer FROM lot l
               JOIN opportunity o ON o.id = l.opportunity_id LEFT JOIN buyer b ON b.id = o.buyer_id WHERE l.id = %s""", (lot_id,)).fetchone()
        if not lot:
            raise HTTPException(404, "lot not found")
        siblings = conn.execute("SELECT id, lot_no, title, status FROM lot WHERE opportunity_id = %s ORDER BY lot_no", (lot["opportunity_id"],)).fetchall()
    return {**lot, "lots": siblings}


def _readable_document(conn, document_id: int, profile_id: int) -> dict:
    d = conn.execute("""SELECT * FROM document WHERE id = %s AND (owner_profile_id IS NULL OR owner_profile_id = %s)""",
                     (document_id, profile_id)).fetchone()
    if not d:
        raise HTTPException(404, "document not found")
    return d


@router.get("/passages/{passage_id}")
def passage(passage_id: int, profile_id: int = Depends(current_profile)):
    """A cited passage with its neighbours, so a quote can be read in context in any format, including DOCX and notice fields."""
    with connect() as conn:
        p = conn.execute("SELECT id, document_id, ordinal FROM passage WHERE id = %s", (passage_id,)).fetchone()
        if not p:
            raise HTTPException(404, "passage not found")
        d = _readable_document(conn, p["document_id"], profile_id)
        rows = conn.execute(
            """SELECT id, ordinal, page, section, text, kind FROM passage WHERE document_id = %s AND abs(ordinal - %s) <= 1 ORDER BY ordinal""",
            (p["document_id"], p["ordinal"])).fetchall()
    return {"document": {"id": d["id"], "filename": d["filename"], "origin": d["origin"], "has_file": bool(d["storage_path"]),
                         "content_type": d["content_type"], "url": d["url"]}, "passage_id": passage_id, "passages": rows}


@router.get("/documents/{document_id}/file")
def document_file(document_id: int, profile_id: int = Depends(current_profile)):
    with connect() as conn:
        d = _readable_document(conn, document_id, profile_id)
    path = Path(d["storage_path"] or "")
    root = Path(settings.document_dir).resolve()
    if not d["storage_path"] or not path.resolve().is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "this document has no stored file")
    pdf = path.suffix == ".pdf"
    return FileResponse(path, media_type="application/pdf" if pdf else "application/octet-stream", filename=d["filename"] or path.name,
                        content_disposition_type="inline" if pdf else "attachment")


@router.get("/lots/{lot_id}/preparation")
def preparation(lot_id: int, profile_id: int = Depends(current_profile)):
    from tenderfit.api.assessments import get_assessment

    a = get_assessment(lot_id, profile_id)
    if a["assessment_id"] is None:
        raise HTTPException(404, "no assessment yet; POST /lots/{id}/assess")
    with connect() as conn:
        decision = conn.execute("SELECT * FROM lot_decision WHERE lot_id = %s AND profile_id = %s", (lot_id, profile_id)).fetchone()
        docs = [d for d in inventory(conn, a["version_id"], profile_id)["documents"] if d["kind"] != "notice"]
    deadline = a["lot"]["deadline"]
    start = (decision or {}).get("created_at") or datetime.now(timezone.utc)
    milestones = [{"label": "Decide to pursue", "date": start, "done": bool(decision and decision["decision"] == "pursue")}]
    if deadline:
        span = deadline - start
        milestones += [{"label": "Gather missing evidence", "date": start + span * 0.3, "done": False},
                       {"label": "Send clarification questions", "date": start + span * 0.5, "done": False,
                        "note": "check the buyer's clarification cut-off in the ITT"},
                       {"label": "Internal review", "date": deadline - timedelta(days=3), "done": False},
                       {"label": "Submission deadline", "date": deadline, "done": False, "final": True}]
    provide, confirm_inside, clarify, sections = [], [], [], []
    for r in a["requirements"]:
        src = f"{r['document']} {r['section'] or ''}".strip() + (f" · p.{r['page']}" if r["page"] else "")
        if r["obligation"] == "scored":
            sections.append({"label": r["statement"], "source": src})
            continue
        if r["needs_human"] in ("G3",):
            confirm_inside.append({"label": r["question"] or r["statement"], "owner": "Operations", "source": src})
        elif r["status"] in ("unknown", "needs_confirmation"):
            ready = any(f["evidence_status"] == "document_on_file" for f in r["facts"])
            provide.append({"label": r["statement"], "source": src,
                            "state": "ready" if ready else ("missing" if r["status"] == "unknown" else "request")})
        else:
            provide.append({"label": r["statement"], "source": src, "state": "ready"})
        if r["equivalent_allowed"] and r["status"] != "met":
            clarify.append({"source": src, "text": (
                f"{r['section'] or 'The specification'} allows \"an equivalent\" for: {r['statement']}. Please confirm which "
                "standards are accepted as equivalent, and whether a manufacturer's test report from an accredited "
                "laboratory is sufficient evidence."), "status": "draft for your review"})
    for d in docs:
        if d["status"] not in ("read", "confirmed_none"):
            provide.append({"label": d["label"], "source": d["referenced_in"] or "Notice: documents",
                            "state": "you prepare" if not d["essential"] else "missing"})
    return {"lot": a["lot"], "recommendation": a["recommendation"], "decision": decision, "milestones": milestones,
            "documents": provide, "confirm_inside": confirm_inside, "clarifications": clarify, "response_sections": sections,
            "note": "TenderFit will not price, commit or submit for you; you submit on the buyer's portal."}


@router.get("/profile/evidence-impact")
def evidence_impact(profile_id: int = Depends(current_profile)):
    with connect() as conn:
        rows = conn.execute(
            """SELECT f.id, f.label, f.evidence_status, f.needs_update_reason,
                      jsonb_agg(DISTINCT jsonb_build_object('lot_id', a.lot_id, 'opportunity', o.title, 'buyer', b.name,
                                                            'lot_no', l.lot_no, 'obligation', r.obligation)) AS used_by
               FROM profile_fact f JOIN verdict_fact vf ON vf.fact_id = f.id JOIN verdict v ON v.id = vf.verdict_id
               JOIN assessment a ON a.id = v.assessment_id AND NOT a.superseded AND NOT a.shadow
               JOIN requirement r ON r.id = v.requirement_id JOIN lot l ON l.id = a.lot_id
               JOIN opportunity o ON o.id = l.opportunity_id LEFT JOIN buyer b ON b.id = o.buyer_id
               WHERE f.profile_id = %s AND v.status <> 'met'
                 AND (f.evidence_status IN ('missing', 'unknown', 'needs_update') OR v.needs_human = 'G2')
               GROUP BY f.id ORDER BY count(*) DESC""", (profile_id,)).fetchall()
    return rows


@router.get("/profile/pursued")
def pursued(profile_id: int = Depends(current_profile)):
    with connect() as conn:
        return conn.execute(
            """SELECT d.lot_id, d.created_at, l.lot_no, l.title AS lot_title, o.title, o.deadline, b.name AS buyer
               FROM lot_decision d JOIN lot l ON l.id = d.lot_id JOIN opportunity o ON o.id = l.opportunity_id
               LEFT JOIN buyer b ON b.id = o.buyer_id WHERE d.profile_id = %s AND d.decision = 'pursue'
               ORDER BY o.deadline NULLS LAST""", (profile_id,)).fetchall()
