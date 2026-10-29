from tenderfit.assessment.confirm import confirm
from tenderfit.assessment.pipeline import assess_lot
from tenderfit.changes.apply import process_pending, shadow_full
from tenderfit.documents import service
from tenderfit.ingest.importer import run_import
from tenderfit.profile.seed import seed
from tests import northfield
from tests.fakes import ScriptedLLM
from tests.test_importer import NOW, T0, T1, fake
from tests.test_pipeline import TODAY, portal, verdicts
from tests.test_retrieval import BagEmbedder, OverlapReranker


def setup_v1(db, tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lot1 = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    v1 = db.execute("SELECT current_version_id v FROM opportunity").fetchone()["v"]
    service.process_version(db, v1, portal(northfield.pdf(northfield.SPEC_V1)))
    itt = next(x for x in service.inventory(db, v1)["documents"] if x["label"] == "Instructions to Tenderers")
    service.attach_upload(db, v1, "ITT.docx", northfield.itt_docx(), "sam", itt["id"])
    for x in service.inventory(db, v1)["documents"]:
        if x["kind"] == "referenced":
            confirm(db, "G7", "sam", x["id"], "none_issued")
    llm = ScriptedLLM(verdicts)
    kw = dict(today=TODAY, emb=BagEmbedder(), rr=OverlapReranker())
    a1 = assess_lot(db, lot1, atlas, llm, **kw)
    return atlas, lot1, v1, llm, kw, a1


def test_amendment_is_reassessed_through_dependencies(db, tmp_path, monkeypatch):
    atlas, lot1, v1, llm, kw, a1 = setup_v1(db, tmp_path, monkeypatch)
    assert db.execute("SELECT recommendation FROM assessment WHERE id = %s", (a1,)).fetchone()["recommendation"].startswith("Blocker found")
    confirm(db, "G1", "sam", lot1, "pursue", profile_id=atlas)

    v2 = northfield.release(rid="029400-2026", tag="tenderUpdate", date="2026-10-12T18:00:00Z",
                            deadline="2026-11-06T12:00:00Z", spec_url="https://portal.test/Spec_Desks_v2.pdf")
    run_import(db, fake([v2]), T0, T1, now=NOW)
    calls_before = len(llm.users)
    [impact_id] = process_pending(db, llm, http=portal(northfield.pdf(northfield.SPEC_V2)), **kw)

    ch = db.execute("SELECT * FROM change_event WHERE kind = 'version'").fetchone()
    assert ch["cls"] == "local", ch["reasons"]
    assert {f["path"] for f in ch["fields"]} == {"notice.deadline"}
    assert sorted((p["kind"], p["section"]) for p in ch["passages"]) == [("modified", "3.2 Delivery"),
                                                                          ("modified", "Notice: submission")]

    imp = db.execute("SELECT * FROM change_impact WHERE id = %s", (impact_id,)).fetchone()
    assert imp["mode"] == "targeted" and imp["kept"] == 4
    by = {a["statement"]: a for a in imp["affected"]}
    assert set(by) == {"Deliver all desks within 45 days of order", "Comparable contract (150+ items, last 3 years) with reference"}
    assert (by["Deliver all desks within 45 days of order"]["before"], by["Deliver all desks within 45 days of order"]["after"]) == \
        ("not_met", "needs_confirmation")
    assert by["Comparable contract (150+ items, last 3 years) with reference"]["how"] == "rule only"
    assert len(llm.users) == calls_before + 1, "only the edited passage is re-extracted; no verdict model calls"
    assert "notice.deadline" in imp["explanation"] and "Recommendation: Blocker found" in imp["explanation"]

    after = db.execute("SELECT * FROM assessment WHERE id = %s", (imp["after_assessment_id"],)).fetchone()
    assert after["coverage"] == "complete"
    assert after["recommendation"] == "Relevant: 3 items need confirmation before you bid"
    before = db.execute("SELECT superseded FROM assessment WHERE id = %s", (a1,)).fetchone()
    assert before["superseded"]

    job = db.execute("SELECT * FROM job WHERE kind = 'shadow_full'").fetchone()
    audit = shadow_full(db, job["payload"]["impact_id"], llm, **kw)
    assert audit == {"agrees": True, "missed_dependencies": []}
    assert db.execute("SELECT count(*) n FROM assessment WHERE NOT superseded").fetchone()["n"] == 1


def test_lot_split_triggers_full_reassessment(db, tmp_path, monkeypatch):
    atlas, lot1, v1, llm, kw, a1 = setup_v1(db, tmp_path, monkeypatch)
    v2 = northfield.release(rid="029500-2026", tag="tenderUpdate", date="2026-10-12T18:00:00Z")
    v2["tender"]["lots"].append({"id": "3", "title": "Lockers", "status": "active"})
    run_import(db, fake([v2]), T0, T1, now=NOW)
    [impact_id] = process_pending(db, llm, http=portal(northfield.pdf(northfield.SPEC_V1)), **kw)
    imp = db.execute("SELECT mode, reason, explanation FROM change_impact WHERE id = %s", (impact_id,)).fetchone()
    assert imp["mode"] == "full" and "lots added" in imp["reason"]
    assert imp["explanation"].startswith("Full reassessment of the lot")


def test_profile_fact_change_reaches_verdicts_that_used_it(db, tmp_path, monkeypatch):
    from tenderfit.profile.service import update_fact, verdicts_using
    atlas, lot1, v1, llm, kw, a1 = setup_v1(db, tmp_path, monkeypatch)
    cap = db.execute("SELECT id FROM profile_fact WHERE profile_id = %s AND attribute = 'capacity'", (atlas,)).fetchone()["id"]
    used = verdicts_using(db, [cap])
    statements = {r["statement"] for r in db.execute(
        "SELECT r.statement FROM verdict v JOIN requirement r ON r.id = v.requirement_id WHERE v.id = ANY(%s)", (used,))}
    assert statements == {"Deliver all desks within 30 days of order", "Supply 200 student desks"}
    assert set(update_fact(db, cap, value={"max_units": 300, "lead_days": 28})) == set(used)



def test_changes_endpoint_and_profile_change(db, _test_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tenderfit.api.main import app
    from tenderfit.changes.apply import apply_profile_change
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    atlas, lot1, v1, llm, kw, a1 = setup_v1(db, tmp_path, monkeypatch)
    cap = db.execute("SELECT id FROM profile_fact WHERE profile_id = %s AND attribute = 'capacity'", (atlas,)).fetchone()["id"]
    db.execute("UPDATE profile_fact SET value = %s WHERE id = %s", ('{"max_units": 300, "lead_days": 28}', cap))
    db.commit()
    calls = len(llm.users)
    aid = apply_profile_change(db, lot1, atlas, [cap], llm, "lead time changed to 28 days", **kw)
    assert len(llm.users) == calls
    rec = db.execute("SELECT recommendation FROM assessment WHERE id = %s", (aid,)).fetchone()["recommendation"]
    assert not rec.startswith("Blocker found")
    with TestClient(app) as c:
        [ev] = c.get("/changes", params={"profile_id": atlas}).json()
        assert ev["kind"] == "profile" and ev["impacts"][0]["kept"] == 4
        moved = {a["statement"]: (a["before"], a["after"]) for a in ev["impacts"][0]["affected"]}
        assert moved["Deliver all desks within 30 days of order"] == ("not_met", "needs_confirmation")
