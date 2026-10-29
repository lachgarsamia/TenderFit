from fastapi.testclient import TestClient

from tenderfit.api.main import app
from tenderfit.assessment.pipeline import assess_lot
from tenderfit.documents import service
from tenderfit.ingest.importer import run_import
from tenderfit.profile.seed import seed
from tests import northfield
from tests.fakes import ScriptedLLM
from tests.test_importer import NOW, T0, T1, fake
from tests.test_pipeline import TODAY, portal, verdicts
from tests.test_retrieval import BagEmbedder, OverlapReranker


def test_assessment_endpoint_and_confirmations(db, _test_db, tmp_path, monkeypatch):
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lot1 = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    kw = dict(llm=ScriptedLLM(verdicts), today=TODAY, emb=BagEmbedder(), rr=OverlapReranker())
    assess_lot(db, lot1, atlas, http=portal(northfield.pdf(northfield.SPEC_V1)), **kw)

    with TestClient(app) as c:
        a = c.get(f"/lots/{lot1}/assessment", params={"profile_id": atlas}).json()
        assert a["recommendation"].startswith("Blocker found")
        assert a["status"]["status"] in ("open", "unverified") and a["on_current_version"]
        delivery = next(r for r in a["requirements"] if r["statement"].startswith("Deliver all desks"))
        assert delivery["citations"][0]["page"] == 6 and delivery["facts"][0]["label"].startswith("Up to 300 desks")
        assert delivery["document"] == "Spec_Desks_v1.pdf"

        r = c.post("/confirmations", json={"point": "G6", "target_id": delivery["id"], "decision": "override", "user": "sam"})
        assert r.status_code == 422 and "written reason" in r.json()["detail"]
        r = c.post("/confirmations", json={"point": "G6", "target_id": delivery["id"], "decision": "override", "user": "sam",
                                           "reason": "Buyer confirmed by phone that 45 days is acceptable"})
        assert r.status_code == 200 and len(r.json()["jobs"]) == 1

        inv = c.get(f"/lots/{lot1}/documents").json()
        itt = next(d for d in inv["documents"] if d["label"] == "Instructions to Tenderers")
        assert c.post("/confirmations", json={"point": "G7", "target_id": itt["id"], "decision": "none_issued"}).status_code == 200
        assert c.post("/confirmations", json={"point": "G1", "target_id": lot1, "decision": "pursue",
                                              "profile_id": atlas}).status_code == 200

    assess_lot(db, lot1, atlas, **kw)
    with TestClient(app) as c:
        a = c.get(f"/lots/{lot1}/assessment", params={"profile_id": atlas}).json()
        delivery = next(r for r in a["requirements"] if r["statement"].startswith("Deliver all desks"))
        assert delivery["status"] == "not_met" and delivery["override_status"] == "needs_confirmation"
        assert delivery["override_by"] == "sam"
        assert not a["recommendation"].startswith("Blocker found")
        assert a["decision"]["decision"] == "pursue"
        points = [r["point"] for r in db.execute("SELECT point FROM confirmation ORDER BY id")]
        assert points == ["G6", "G7", "G1"]
        assert c.get(f"/assessments/{a['assessment_id']}/trace").json()["verdicts"]
