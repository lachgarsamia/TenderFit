import json
import re
from datetime import date

import httpx

from tenderfit.assessment.pipeline import assess_lot
from tenderfit.documents import service
from tenderfit.ingest.importer import run_import
from tenderfit.profile import service as profiles
from tenderfit.profile.seed import seed
from tests import northfield
from tests.fakes import ScriptedLLM, passage_id
from tests.test_extraction import req
from tests.test_importer import NOW, T0, T1, fake
from tests.test_retrieval import BagEmbedder, OverlapReranker

TODAY = date(2026, 10, 14)
NO_COND = None


def cond(kind, fact, **kw):
    return {"kind": kind, "fact": fact, "bound": None, "years": None, "anchor": None, "min_units": None, "match": None, **kw}


def extraction(role, user, schema):
    out = []
    for days in ("30", "45"):
        if f"within {days} days" in user:
            out.append(req(passage_id(user, f"within {days} days"), f"Deliver all desks within {days} days of order",
                           f"The supplier shall deliver all desks within {days} days of order.",
                           condition=cond("at_most", "capacity.lead_days", bound=int(days)), references=["Appendix B"]))
    if "200 student desks" in user:
        out.append(req(passage_id(user, "200 student desks"), "Supply 200 student desks",
                       "The supplier shall supply 200 student desks for Lot 1.", category="quantity",
                       condition=cond("at_least", "capacity.max_units", bound="lot.quantity")))
    if "Installation of all desks" in user:
        out.append(req(passage_id(user, "Installation of all desks"), "Installation included",
                       "Installation of all desks is included in the contract price.", category="other"))
    if "BS EN 1729-2" in user:
        out.append(req(passage_id(user, "BS EN 1729-2"), "Desks meet BS EN 1729-2 or an accepted equivalent",
                       "Desks shall comply with BS EN 1729-2 or an equivalent standard accepted by the University.",
                       category="standard", equivalent_allowed=True, evidence_needed=["test report"],
                       condition=cond("has_evidence", "test_report", match="BS EN 1729-2")))
    if "comparable contract" in user:
        out.append(req(passage_id(user, "comparable contract"), "Comparable contract (150+ items, last 3 years) with reference",
                       "at least one comparable contract for 150 or more items delivered in the last three years, with a customer reference",
                       category="experience", evidence_needed=["customer reference"],
                       condition=cond("within_years", "experience.end", years=3, anchor="notice.deadline", min_units=150)))
    if "recycled material" in user:
        out.append(req(passage_id(user, "recycled material"), "Share of recycled material (scored, 10%)",
                       "The share of recycled material in the desks will be scored", category="other", obligation="scored"))
    return {"requirements": out}


def facts_in(user):
    return json.loads(user.split("validity):\n", 1)[1].split("\n\nPassages:", 1)[0])


def verdicts(role, user, schema):
    if role == "extract":
        return extraction(role, user, schema)
    facts = facts_in(user)
    pid = int(re.search(r"\(passage (\d+)\)", user).group(1))
    if "Installation included" in user:
        inst = [f["id"] for f in facts if f["attribute"] == "service"]
        return {"status": "met", "reason": "Installation service offered (declared)", "supporting_fact_ids": inst,
                "contrary_fact_ids": [], "question": None, "conflict": False,
                "citations": [{"passage_id": pid, "quote": "Installation of all desks is included in the contract price."}]}
    return {"status": "not_met", "reason": "No recycled content figure in profile", "supporting_fact_ids": [],
            "contrary_fact_ids": [], "question": "What share of ND-120 is recycled material?", "conflict": False,
            "citations": [{"passage_id": pid, "quote": "this sentence is not in the passage at all, invented"}]}


def portal(spec_pdf):
    def handler(r):
        if r.url.path.endswith(".pdf"):
            return httpx.Response(200, content=spec_pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(200, html="<html>Register to view</html>")
    return httpx.Client(transport=httpx.MockTransport(handler))


def run(db, aid):
    a = db.execute("SELECT * FROM assessment WHERE id = %s", (aid,)).fetchone()
    vs = {r["statement"]: r for r in db.execute(
        """SELECT r.statement, v.* FROM verdict v JOIN requirement r ON r.id = v.requirement_id WHERE v.assessment_id = %s""",
        (aid,))}
    return a, vs


def test_running_example_v1_then_v2(db, tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lot1 = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    llm = ScriptedLLM(verdicts)
    kw = dict(llm=llm, today=TODAY, emb=BagEmbedder(), rr=OverlapReranker())

    aid = assess_lot(db, lot1, atlas, http=portal(northfield.pdf(northfield.SPEC_V1)), **kw)
    a, vs = run(db, aid)
    assert a["coverage"] == "partial" and a["provisional"] is False  # blocker found is final whatever the coverage
    assert a["recommendation"].startswith("Blocker found: Deliver all desks within 30 days of order")
    d = vs["Deliver all desks within 30 days of order"]
    assert (d["status"], d["decided_by"], d["needs_human"]) == ("not_met", "rule", "G6")
    assert vs["Supply 200 student desks"]["status"] == "met" and vs["Supply 200 student desks"]["basis"] == "declared"
    assert vs["Desks meet BS EN 1729-2 or an accepted equivalent"]["status"] == "unknown"
    inst = vs["Installation included"]
    assert (inst["status"], inst["decided_by"], inst["basis"]) == ("met", "model", "declared")
    assert a["dimensions"]["eligibility"]["state"] == "blocker_found"
    assert all(v["provisional"] for v in vs.values())

    v2 = northfield.release(rid="029400-2026", tag="tenderUpdate", date="2026-10-12T18:00:00Z", deadline="2026-11-06T12:00:00Z",
                            spec_url="https://portal.test/Spec_Desks_v2.pdf")
    run_import(db, fake([v2]), T0, T1, now=NOW)
    vid2 = db.execute("SELECT current_version_id v FROM opportunity").fetchone()["v"]
    service.process_version(db, vid2, portal(northfield.pdf(northfield.SPEC_V2)))
    itt = next(x for x in service.inventory(db, vid2)["documents"] if x["label"] == "Instructions to Tenderers")
    service.attach_upload(db, vid2, "ITT.docx", northfield.itt_docx(), "sam", itt["id"])
    aid2 = assess_lot(db, lot1, atlas, **kw)
    a2, vs2 = run(db, aid2)
    assert a2["coverage"] == "partial" and a2["recommendation"].startswith("Provisional: no blocker found in documents read")
    assert a2["dimensions"]["eligibility"]["label"] == "Provisional"
    d2 = vs2["Deliver all desks within 45 days of order"]
    assert (d2["status"], d2["needs_human"]) == ("needs_confirmation", "G3")
    exp = vs2["Comparable contract (150+ items, last 3 years) with reference"]
    assert (exp["status"], exp["needs_human"]) == ("needs_confirmation", "G2")
    rec = vs2["Share of recycled material (scored, 10%)"]
    assert rec["status"] == "unknown" and rec["decided_by"] == "guard" and "citation rejected" in rec["escalate_reason"]
    cites = db.execute("SELECT quote FROM verdict_citation WHERE verdict_id = %s", (rec["id"],)).fetchall()
    assert [c["quote"] for c in cites] == ["The share of recycled material in the desks will be scored"]

    for x in service.inventory(db, vid2)["documents"]:
        if x["kind"] == "referenced":
            db.execute("UPDATE document_ref SET status = 'confirmed_none' WHERE id = %s", (x["id"],))
    calls_before = len(llm.users)
    a3, _ = run(db, assess_lot(db, lot1, atlas, **kw))
    assert a3["coverage"] == "complete"
    assert a3["recommendation"] == "Relevant: 3 items need confirmation before you bid"
    assert len(llm.users) == calls_before, "unchanged requirements must come from the verdict cache"
    assert db.execute("SELECT count(*) n FROM assessment WHERE NOT superseded").fetchone()["n"] == 1


def test_budget_reached_is_never_a_guess(db, tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    monkeypatch.setattr("tenderfit.assessment.pipeline.settings.lot_cost_cap_usd", 0.0)
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lot1 = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    aid = assess_lot(db, lot1, atlas, llm=ScriptedLLM(verdicts), today=TODAY, emb=BagEmbedder(), rr=OverlapReranker(),
                     http=portal(northfield.pdf(northfield.SPEC_V1)))
    a, vs = run(db, aid)
    inst = vs["Installation included"]
    assert inst["decided_by"] == "budget" and inst["reason"].startswith("budget reached")
    assert inst["status"] in ("needs_confirmation", "unknown") and a["budget_reached"]
    assert vs["Deliver all desks within 30 days of order"]["status"] == "not_met"


def test_document_uploaded_after_an_assessment_is_extracted(db, tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    ids = seed(db)
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lots = {r["lot_no"]: r["id"] for r in db.execute("SELECT id, lot_no FROM lot")}
    kw = dict(llm=ScriptedLLM(verdicts), today=TODAY, emb=BagEmbedder(), rr=OverlapReranker())
    assess_lot(db, lots["2"], ids["Pennine Seating"], http=portal(northfield.pdf(northfield.SPEC_V1)), **kw)
    vid = db.execute("SELECT current_version_id v FROM opportunity").fetchone()["v"]
    itt = next(x for x in service.inventory(db, vid)["documents"] if x["label"] == "Instructions to Tenderers")
    service.attach_upload(db, vid, "ITT.docx", northfield.itt_docx(), "sam", itt["id"])
    a, vs = run(db, assess_lot(db, lots["1"], ids["Atlas Furniture"], **kw))
    assert "Comparable contract (150+ items, last 3 years) with reference" in vs
    assert db.execute("SELECT count(*) n FROM requirement WHERE statement LIKE 'Supply 200%%'").fetchone()["n"] == 1
