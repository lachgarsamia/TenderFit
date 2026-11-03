"""The fictional demonstration runs through the real pipeline, and the endpoints the screens use behave."""
import argparse
import io

import pytest
from fastapi.testclient import TestClient

from tenderfit.api.main import app
from tenderfit.demo.__main__ import SUPPLIERS, amend, load, reset
from tenderfit.demo.llm import DemoLLM
from tenderfit.documents import service
from tenderfit.ingest.importer import run_import
from tenderfit.profile.seed import seed
from tests import northfield
from tests.test_importer import NOW, T0, T1, fake

ARGS = argparse.Namespace(fast=True, fail_first=False, audit=False)


@pytest.fixture
def demo(db, _test_db, tmp_path, monkeypatch):
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    monkeypatch.setattr("tenderfit.agent.loop.settings.agent_enabled", False)
    load(db, ARGS)
    ids = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM business_profile")}
    lots = {r["lot_no"]: r["id"] for r in db.execute("SELECT id, lot_no FROM lot")}
    return db, ids[SUPPLIERS[0]], ids[SUPPLIERS[1]], lots


def reqs(c, lot, profile):
    a = c.get(f"/lots/{lot}/assessment", params={"profile_id": profile}).json()
    return a, {r["statement"]: r for r in a["requirements"]}


def test_demo_covers_every_kind_of_decision(demo):
    db, bright, oak, lots = demo
    with TestClient(app) as c:
        a, r = reqs(c, lots["1"], bright)
    assert r["Deliver all desks within 30 days of order"]["status"] == "not_met"
    assert r["Hold public liability insurance of at least £5,000,000"]["status"] == "met"
    assert r["Hold public liability insurance of at least £5,000,000"]["evidence_quality"] == "strong"
    assert r["Hold employers liability insurance of at least £5,000,000"]["needs_human"] == "G2", "the right policy, but only declared"
    assert r["Supply 200 student desks"]["evidence_quality"] == "weak"
    assert r["Desks comply with BS EN 1729-2 or an accepted equivalent"]["status"] == "unknown"
    iso = r["Hold ISO 14001, valid for the contract, from an accredited body"]
    assert iso["status"] == "needs_confirmation" and len(iso["conditions"]) == 3
    assert {x["decided_by"] for x in iso["conditions"]} == {"rule", "guard"} or "model" in {x["decided_by"] for x in iso["conditions"]}
    inst = r["Install all desks on Working Days"]
    assert any("Appendix B" in w for x in inst["retrieval"] for w in x["why"]), "retrieved because the requirement cites it"
    warranty = r["Provide a warranty of 5 years on all desks"]
    assert warranty["needs_human"] == "G5" and "3 years" in warranty["question"], "the specification and the ITT disagree"
    spares = r["Spare parts available for dispatch within 10 days"]
    assert spares["decided_by"] != "rule" and "14 does not appear" in spares["condition_note"], "the wrong number never became a rule"
    assert any(x["kind"] == "quote" and "25 years" in x["statement"] for x in a["rejections"]), "the invented one is listed, not hidden"
    assert a["recommendation"].startswith("Blocker found")


def test_demo_amendment_changes_two_verdicts_for_the_reasons_shown(demo):
    db, bright, oak, lots = demo
    amend(db, ARGS)
    with TestClient(app) as c:
        a, r = reqs(c, lots["1"], bright)
        events = c.get("/changes", params={"profile_id": bright}).json()
    assert r["Deliver all desks within 45 days of order"]["status"] == "needs_confirmation"
    ins = r["Hold public liability insurance of at least £5,000,000"]
    assert ins["status"] == "needs_confirmation" and "before the submission deadline" in ins["reason"]
    assert r["Supply 200 student desks"]["carried_over"] is True
    [ev] = [e for e in events if e["kind"] == "version"]
    assert (ev["status"], ev["from_version_no"], ev["to_version_no"]) == ("applied", 1, 2)
    assert {i["lot_no"] for i in ev["impacts"]} == {"1", "2"}, "all affected lots are reported"
    assert not a["recommendation"].startswith("Blocker")


def test_demo_failed_amendment_then_retry(demo):
    db, bright, oak, lots = demo
    amend(db, argparse.Namespace(fast=True, fail_first=True, audit=False))
    with TestClient(app) as c:
        [ev] = [e for e in c.get("/changes", params={"profile_id": bright}).json() if e["kind"] == "version"]
        assert ev["status"] == "failed" and "overloaded" in ev["last_error"]
        assert c.get(f"/lots/{lots['1']}/assessment", params={"profile_id": bright}).json()["on_current_version"] is False
    DemoLLM.failing = 0
    amend(db, ARGS)
    with TestClient(app) as c:
        [ev] = [e for e in c.get("/changes", params={"profile_id": bright}).json() if e["kind"] == "version"]
        assert (ev["status"], ev["attempts"]) == ("applied", 2)


def test_targeted_result_agrees_with_full_reassessment_on_the_demo(demo):
    from tenderfit.changes.apply import shadow_full
    from tenderfit.demo.__main__ import HashEmbedder, OverlapReranker, portal
    db, bright, oak, lots = demo
    amend(db, ARGS)
    for row in db.execute("SELECT id FROM change_impact").fetchall():
        audit = shadow_full(db, row["id"], DemoLLM(), http=portal(45), agent=False, emb=HashEmbedder(), rr=OverlapReranker())
        assert audit["agrees"], audit


def test_reset_removes_only_the_fictional_tender(demo):
    db, bright, oak, lots = demo
    amend(db, ARGS)
    run_import(db, fake([northfield.release(rid="1-1").copy() | {"ocid": "ocds-real-1"}]), T0, T1, now=NOW)
    reset(db, ARGS)
    assert [r["ocid"] for r in db.execute("SELECT ocid FROM opportunity")] == ["ocds-real-1"]


def test_tender_page_reaches_all_lots_and_the_supplier_only_sees_its_own_documents(demo):
    db, bright, oak, lots = demo
    opp = db.execute("SELECT id FROM opportunity").fetchone()["id"]
    with TestClient(app) as c:
        t = c.get(f"/tenders/{opp}", params={"profile_id": bright}).json()
        t2 = c.get(f"/tenders/{opp}", params={"profile_id": oak}).json()
        assert c.get("/tenders/999999", params={"profile_id": bright}).status_code == 404
        s = c.get(f"/lots/{lots['2']}").json()
    assert [l["lot_no"] for l in t["lots"]] == ["1", "2"] and t["lots"][0]["fit"]["assessed"]
    assert t["fictional"] and t["versions"][0]["version_no"] == 1 and s["opportunity_id"] == opp and len(s["lots"]) == 2
    status = lambda x: next(d for d in x["documents"] if d["label"] == "Instructions to Tenderers")["status"]  # noqa: E731
    assert status(t) == "read" and status(t2) == "not_retrieved"


def test_source_navigation_respects_ownership(demo):
    db, bright, oak, lots = demo
    private = db.execute("SELECT d.id, p.id AS pid FROM document d JOIN passage p ON p.document_id = d.id WHERE d.owner_profile_id = %s LIMIT 1", (bright,)).fetchone()
    public = db.execute("SELECT d.id, p.id AS pid FROM document d JOIN passage p ON p.document_id = d.id WHERE d.filename LIKE 'Specification%%' LIMIT 1").fetchone()
    with TestClient(app) as c:
        assert c.get(f"/passages/{private['pid']}", params={"profile_id": bright}).status_code == 200
        assert c.get(f"/passages/{private['pid']}", params={"profile_id": oak}).status_code == 404
        assert c.get(f"/passages/{private['pid']}").status_code == 404
        assert c.get(f"/documents/{private['id']}/file", params={"profile_id": oak}).status_code == 404
        pdf = c.get(f"/documents/{public['id']}/file")
        assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
        ctx = c.get(f"/passages/{public['pid']}").json()
        assert ctx["passages"] and ctx["document"]["has_file"]


def test_search_defaults_to_the_suppliers_own_sector_and_open_tenders_are_not_crowded_out(db, _test_db, tmp_path, monkeypatch):
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    releases = []
    for i in range(6):
        releases.append({**northfield.release(rid=f"closed-{i}", deadline="2020-01-01T12:00:00Z"), "ocid": f"ocds-closed-{i}"})
    releases.append({**northfield.release(rid="open-1", deadline="2099-01-01T12:00:00Z"), "ocid": "ocds-open-1"})
    other = northfield.release(rid="open-2", deadline="2099-01-01T12:00:00Z")
    other["ocid"] = "ocds-open-2"
    other["tender"]["title"] = "Grounds maintenance services"
    for item in other["tender"]["items"]:
        item["additionalClassifications"] = [{"scheme": "CPV", "id": "77310000"}]
    releases.append(other)
    run_import(db, fake(releases), T0, T1, now=NOW)
    with TestClient(app) as c:
        mine = c.post("/search", json={"q": "", "profile_id": atlas, "limit": 1}).json()
        assert [o["ocid"] for o in mine["shortlist"]] == ["ocds-open-1"] and mine["parsed"]["from_profile"]
        words = c.post("/search", json={"q": "grounds maintenance", "profile_id": atlas, "limit": 5}).json()
        assert [o["ocid"] for o in words["shortlist"]] == ["ocds-open-2"], "any sector can be searched by its words"


def test_unexpected_errors_return_structured_json(demo, monkeypatch):
    db, bright, oak, lots = demo
    monkeypatch.setattr("tenderfit.api.assessments.profiles.facts", lambda *a: 1 / 0)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/profile/facts", params={"profile_id": bright})
    assert r.status_code == 500 and "unexpected error" in r.json()["detail"]


def test_private_upload_to_a_missing_reference_is_refused(demo):
    db, bright, oak, lots = demo
    with TestClient(app) as c:
        r = c.post(f"/lots/{lots['1']}/documents", data={"profile_id": bright, "ref_id": 99999999},
                   files={"file": ("a.pdf", io.BytesIO(b"%PDF-1.4 minimal"))})
    assert r.status_code == 404
