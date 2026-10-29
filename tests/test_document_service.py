from datetime import datetime, timezone

import httpx

from tenderfit.documents import service
from tenderfit.ingest.importer import run_import
from tests import northfield
from tests.test_importer import NOW, T0, T1, fake


def _version(db):
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    return db.execute("SELECT id FROM notice_version").fetchone()["id"]


def _portal(tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    pdf = northfield.pdf(northfield.SPEC_V1)

    def handler(req):
        if req.url.path.endswith(".pdf"):
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(200, html="<html>Register to view documents</html>")
    return httpx.Client(transport=httpx.MockTransport(handler))


def refs(db, vid):
    return {r["label"]: (r["kind"], r["status"], r["essential"]) for r in service.inventory(db, vid)["documents"]}


def test_inventory_coverage_and_upload(db, tmp_path, monkeypatch):
    vid = _version(db)
    service.process_version(db, vid, _portal(tmp_path, monkeypatch))
    inv = service.inventory(db, vid)
    r = refs(db, vid)
    assert r["Tender notice"] == ("notice", "read", True)
    assert r["Specification"] == ("document", "read", True)
    assert r["Instructions to Tenderers"] == ("document", "not_retrieved", True)
    assert r["Pricing schedule"] == ("document", "not_retrieved", False)
    assert r["Appendix B"] == ("referenced", "not_retrieved", True)
    assert inv["coverage"] == "partial" and (inv["essential_read"], inv["essential_total"]) == (1, 3)

    itt = next(x for x in inv["documents"] if x["label"] == "Instructions to Tenderers")
    service.attach_upload(db, vid, "ITT.docx", northfield.itt_docx(), "sam", itt["id"])
    r = refs(db, vid)
    assert r["Instructions to Tenderers"][1] == "read" and r["Appendix C"][0] == "referenced"

    for x in service.inventory(db, vid)["documents"]:
        if x["kind"] == "referenced":
            db.execute("UPDATE document_ref SET status = 'confirmed_none' WHERE id = %s", (x["id"],))
    assert service.inventory(db, vid)["coverage"] == "complete"


def test_notice_fields_become_citable_passages(db, tmp_path, monkeypatch):
    vid = _version(db)
    service.ensure_inventory(db, vid)
    rows = db.execute("""SELECT p.section, p.text, p.kind FROM passage p JOIN document d ON d.id = p.document_id
                         WHERE d.version_id = %s""", (vid,)).fetchall()
    sections = {r["section"] for r in rows}
    assert {"Notice: lot 1", "Notice: conditions of participation"} <= sections
    assert any("£5m public liability" in r["text"] for r in rows) and {r["kind"] for r in rows} == {"field"}
