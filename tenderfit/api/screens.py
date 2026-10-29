import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tenderfit.db import connect
from tenderfit.rules import aggregate as agg

router = APIRouter()

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
           FROM assessment WHERE lot_id = ANY(%s) AND profile_id = %s AND NOT superseded AND NOT shadow
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
    q: str = ""
    profile_id: int
    cpv: list[str] | None = None
    regions: list[str] | None = None
    min_days: int = 0
    limit: int = 30


@router.post("/search")
def search(body: SearchIn):
    from tenderfit.api.main import effective_status

    parsed = parse_request(body.q)
    cpv = body.cpv if body.cpv is not None else parsed["cpv"]
    regions = body.regions if body.regions is not None else parsed["regions"]
    now = datetime.now(timezone.utc)
    with connect() as conn:
        profile = _profile(conn, body.profile_id)
        where, args = ["TRUE"], []
        if cpv:
            where.append("EXISTS (SELECT 1 FROM unnest(o.cpv_codes) c WHERE c LIKE ANY(%s))")
            args.append([f"{c}%" for c in cpv])
        if regions:
            where.append("(o.regions = '{}' OR EXISTS (SELECT 1 FROM unnest(o.regions) r WHERE r LIKE ANY(%s)))")
            args.append([f"{r}%" for r in regions])
        if parsed["terms"] and not cpv:
            where.append("to_tsvector('english', coalesce(o.title, '') || ' ' || coalesce(nv.fields->>'description', '')) "
                         "@@ to_tsquery('english', %s)")
            args.append(" | ".join(parsed["terms"]))
        rows = conn.execute(f"""
            SELECT o.id, o.ocid, o.title, o.deadline, o.status, o.status_reason, o.last_checked_at, b.name AS buyer,
                   nv.release_id, nv.published_at, nv.notice_type,
                   (SELECT count(*) FROM notice_version v WHERE v.ocid = o.ocid) AS versions,
                   (SELECT max(v.published_at) FROM notice_version v WHERE v.ocid = o.ocid) AS last_version_at,
                   (SELECT jsonb_agg(jsonb_build_object('id', l.id, 'lot_no', l.lot_no, 'title', l.title, 'status', l.status)
                                     ORDER BY l.lot_no) FROM lot l WHERE l.opportunity_id = o.id) AS lots
            FROM opportunity o LEFT JOIN buyer b ON b.id = o.buyer_id LEFT JOIN notice_version nv ON nv.id = o.current_version_id
            WHERE {' AND '.join(where)}
              AND (o.deadline IS NULL OR o.deadline > now() - interval '30 days')
            ORDER BY o.deadline NULLS LAST LIMIT %s""", (*args, body.limit * 3)).fetchall()
        lot_ids = [l["id"] for r in rows for l in (r["lots"] or [])]
        fits = _fit(conn, lot_ids, body.profile_id, profile) if lot_ids else {}
        assessed_closed = {r["opportunity_id"] for r in conn.execute(
            """SELECT DISTINCT l.opportunity_id FROM assessment a JOIN lot l ON l.id = a.lot_id
               WHERE a.profile_id = %s""", (body.profile_id,))}
        wm = conn.execute("SELECT watermark FROM import_state").fetchone()
    shortlist, gone = [], []
    for r in rows:
        s = effective_status(r["status"], r["status_reason"], r["deadline"], r["last_checked_at"], now)
        item = {**r, "status": s.status, "status_reason": s.reason, "fictional": r["ocid"].startswith("ocds-test-"),
                "lots": [{**l, "fit": fits.get(l["id"])} for l in (r["lots"] or [])]}
        if s.status in ("open", "unverified"):
            if body.min_days and r["deadline"] and r["deadline"] < now + timedelta(days=body.min_days):
                continue
            shortlist.append(item)
        elif r["id"] in assessed_closed:
            gone.append(item)
    order = {"relevant": 0, "provisional": 1, "blocker": 2, "unassessed": 3, "low": 4}
    shortlist.sort(key=lambda o: (min((order.get((l["fit"] or {}).get("kind"), 5) for l in o["lots"]), default=5),
                                  o["deadline"] or now + timedelta(days=999)))
    return {"parsed": {"regions": regions, "cpv": cpv, "terms": parsed["terms"]},
            "last_import": wm and wm["watermark"], "shortlist": shortlist[:body.limit], "no_longer_open": gone}


@router.get("/lots/{lot_id}/preparation")
def preparation(lot_id: int, profile_id: int):
    from tenderfit.api.assessments import get_assessment

    a = get_assessment(lot_id, profile_id)
    with connect() as conn:
        decision = conn.execute("SELECT * FROM lot_decision WHERE lot_id = %s AND profile_id = %s", (lot_id, profile_id)).fetchone()
        docs = conn.execute(
            """SELECT r.label, r.status, r.essential, r.referenced_in FROM document_ref r WHERE r.version_id = %s
               AND r.kind <> 'notice' ORDER BY r.essential DESC, r.id""", (a["version_id"],)).fetchall()
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


@router.get("/profiles/{profile_id}/evidence-impact")
def evidence_impact(profile_id: int):
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


@router.get("/profiles/{profile_id}/pursued")
def pursued(profile_id: int):
    with connect() as conn:
        return conn.execute(
            """SELECT d.lot_id, d.created_at, l.lot_no, l.title AS lot_title, o.title, o.deadline, b.name AS buyer
               FROM lot_decision d JOIN lot l ON l.id = d.lot_id JOIN opportunity o ON o.id = l.opportunity_id
               LEFT JOIN buyer b ON b.id = o.buyer_id WHERE d.profile_id = %s AND d.decision = 'pursue'
               ORDER BY o.deadline NULLS LAST""", (profile_id,)).fetchall()
