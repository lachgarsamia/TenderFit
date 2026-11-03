"""Profile A can never influence profile B: not through answers, uploads, confirmations, caches or reassessment plans."""
import io
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from tenderfit.api.main import app
from tenderfit.assessment.pipeline import assess_lot
from tenderfit.changes.impact import load_deps
from tenderfit.config import settings
from tenderfit.documents import fetch as fetching
from tenderfit.documents import service
from tenderfit.ingest.importer import run_import
from tenderfit.profile.seed import seed
from tests import northfield
from tests.fakes import ScriptedLLM
from tests.test_importer import NOW, T0, T1, fake
from tests.test_pipeline import TODAY, portal, verdicts
from tests.test_retrieval import BagEmbedder, OverlapReranker

KW = dict(today=TODAY, emb=BagEmbedder(), rr=OverlapReranker(), agent=False)


@pytest.fixture
def world(db, _test_db, tmp_path, monkeypatch):
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    ids = seed(db)
    run_import(db, fake([northfield.release(deadline="2099-12-31T12:00:00Z")]), T0, T1, now=NOW)
    lot = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    vid = db.execute("SELECT current_version_id v FROM opportunity").fetchone()["v"]
    llm = ScriptedLLM(verdicts)
    service.process_version(db, vid, portal(northfield.pdf(northfield.SPEC_V1)))
    return db, ids["Atlas Furniture"], ids["Pennine Seating"], lot, vid, llm


def verdict_of(db, aid, statement):
    return db.execute("""SELECT v.*, r.statement FROM verdict v JOIN requirement r ON r.id = v.requirement_id
                         WHERE v.assessment_id = %s AND r.statement LIKE %s""", (aid, statement + "%")).fetchone()


def test_a_suppliers_answer_never_reaches_another_suppliers_prompt(world):
    db, a, b, lot, vid, llm = world
    aid = assess_lot(db, lot, a, llm, **KW)
    v = verdict_of(db, aid, "Share of recycled")
    if v is None:
        service.attach_upload(db, vid, "ITT.docx", northfield.itt_docx(), "operator")
        aid = assess_lot(db, lot, a, llm, **KW)
        v = verdict_of(db, aid, "Share of recycled")
    q = db.execute("SELECT id FROM question WHERE verdict_id = %s", (v["id"],)).fetchone()
    secret = "SECRET-MARGIN-ANSWER-OF-SUPPLIER-A"
    with TestClient(app) as c:
        assert c.post(f"/questions/{q['id']}/answer", json={"answer": secret, "profile_id": b}).status_code == 404, "B cannot answer for A"
        assert c.post(f"/questions/{q['id']}/answer", json={"answer": secret, "profile_id": a}).status_code == 200
    before = len(llm.users)
    assess_lot(db, lot, b, llm, **KW)
    assert all(secret not in u for u in llm.users[before:]), "B's prompts never contain A's answer"
    assess_lot(db, lot, a, llm, **KW)
    assert any(secret in u for u in llm.users[before:]), "A's own later assessment does use it"


def test_a_private_upload_changes_nothing_for_another_supplier(world):
    db, a, b, lot, vid, llm = world
    assess_lot(db, lot, b, llm, **KW)
    b_before = service.inventory(db, vid, b)
    itt = next(d for d in service.inventory(db, vid, a)["documents"] if d["label"] == "Instructions to Tenderers")
    with TestClient(app) as c:
        r = c.post(f"/lots/{lot}/documents", data={"profile_id": a, "ref_id": itt["id"], "user": "a"},
                   files={"file": ("ITT.docx", io.BytesIO(northfield.itt_docx()))})
        assert r.status_code == 200, r.text
        assert r.json()["jobs"] == [], "A has no assessment yet, and B's is not touched"
        shown_b = c.get(f"/lots/{lot}/documents", params={"profile_id": b}).json()
        shown_a = c.get(f"/lots/{lot}/documents", params={"profile_id": a}).json()
        public = c.get(f"/lots/{lot}/documents").json()
    label = "Instructions to Tenderers"
    assert next(d for d in shown_a["documents"] if d["label"] == label)["status"] == "read"
    assert next(d for d in shown_b["documents"] if d["label"] == label)["status"] == "not_retrieved"
    assert next(d for d in public["documents"] if d["label"] == label)["status"] == "not_retrieved"
    assert service.inventory(db, vid, b)["coverage"] == b_before["coverage"]
    assert all(d["label"] != "Appendix C" for d in shown_b["documents"]), "references found in A's file are A's"

    aid = assess_lot(db, lot, a, llm, **KW)
    assert verdict_of(db, aid, "Comparable contract"), "A's private document yields requirements for A"
    bid = assess_lot(db, lot, b, llm, **KW)
    assert not verdict_of(db, bid, "Comparable contract"), "and none for B"
    private = db.execute("SELECT owner_profile_id FROM requirement WHERE statement LIKE 'Comparable contract%%'").fetchone()
    assert private["owner_profile_id"] == a


def test_private_passages_are_not_retrievable_by_others(world):
    from tenderfit.retrieval.search import index_version, search
    db, a, b, lot, vid, llm = world
    service.attach_upload(db, vid, "ITT.docx", northfield.itt_docx(), "a", None, a)
    index_version(db, vid, BagEmbedder())
    q = "comparable contract customer reference Appendix C"
    def texts(pid):
        return " ".join(h.text for h in search(db, vid, q, emb=BagEmbedder(), rr=OverlapReranker(), profile_id=pid, k=20))
    assert "comparable contract" in texts(a)
    assert "comparable contract" not in texts(b)
    assert "comparable contract" not in texts(None)


def test_a_confirmation_only_applies_to_the_supplier_who_made_it(world):
    db, a, b, lot, vid, llm = world
    itt = next(d for d in service.inventory(db, vid)["documents"] if d["label"] == "Instructions to Tenderers")
    aid = assess_lot(db, lot, a, llm, **KW)
    with TestClient(app) as c:
        assert c.post("/confirmations", json={"point": "G7", "target_id": itt["id"], "decision": "none_issued", "profile_id": a}).status_code == 200
        v = verdict_of(db, aid, "Deliver all desks")
        bad = c.post("/confirmations", json={"point": "G6", "target_id": v["id"], "decision": "override", "reason": "x", "profile_id": b})
        assert bad.status_code == 422 and "another profile" in bad.json()["detail"]
    status = lambda p: next(d for d in service.inventory(db, vid, p)["documents"] if d["label"] == "Instructions to Tenderers")["status"]  # noqa: E731
    assert status(a) == "confirmed_none" and status(b) == "not_retrieved" and status(None) == "not_retrieved"


def test_changes_are_scoped_and_other_suppliers_events_do_not_appear(world):
    db, a, b, lot, vid, llm = world
    from tenderfit.changes.apply import process_pending
    assess_lot(db, lot, a, llm, **KW)
    r = northfield.release(rid="029400-2026", tag="tenderUpdate", date="2026-10-12T18:00:00Z", deadline="2099-12-31T12:00:00Z",
                           spec_url="https://portal.test/Spec_Desks_v2.pdf")
    run_import(db, fake([r]), T0, T1, now=NOW)
    process_pending(db, llm, http=portal(northfield.pdf(northfield.SPEC_V2)), **KW)
    with TestClient(app) as c:
        assert len(c.get("/changes", params={"profile_id": a}).json()) == 1
        assert c.get("/changes", params={"profile_id": b}).json() == []
        assert c.get("/changes", params={"profile_id": 999999}).status_code == 404


def test_a_reassessment_plan_uses_only_that_suppliers_dependencies(world):
    db, a, b, lot, vid, llm = world
    aid, bid = assess_lot(db, lot, a, llm, **KW), assess_lot(db, lot, b, llm, **KW)
    facts_a = {str(r["id"]) for r in db.execute("SELECT id FROM profile_fact WHERE profile_id = %s", (a,))}
    facts_b = {str(r["id"]) for r in db.execute("SELECT id FROM profile_fact WHERE profile_id = %s", (b,))}
    dep_a = {d.target_id for d in load_deps(db, aid) if d.kind == "fact"}
    dep_b = {d.target_id for d in load_deps(db, bid) if d.kind == "fact"}
    assert dep_a and dep_a <= facts_a and not dep_a & facts_b
    assert dep_b <= facts_b and not dep_b & facts_a


def test_the_verdict_cache_is_per_supplier(world):
    db, a, b, lot, vid, llm = world
    assess_lot(db, lot, a, llm, **KW)
    n = len(llm.users)
    assess_lot(db, lot, b, llm, **KW)
    assert len(llm.users) > n, "B's model verdicts are B's own, not A's cached ones"


def test_identifiers_are_validated_and_facts_are_owned(world):
    db, a, b, lot, vid, llm = world
    fact = db.execute("SELECT id FROM profile_fact WHERE profile_id = %s LIMIT 1", (a,)).fetchone()["id"]
    with TestClient(app) as c:
        assert c.post(f"/lots/{lot}/assess", json={"profile_id": 999999}).status_code == 404
        assert c.get(f"/lots/{lot}/assessment", params={"profile_id": 999999}).status_code == 404
        assert c.get("/profile/facts", params={"profile_id": 999999}).status_code == 404
        assert c.put(f"/profile/facts/{fact}", params={"profile_id": b}, json={"label": "hijacked"}).status_code == 404
        assert c.post(f"/profile/facts/{fact}/verify", json={"profile_id": b}).status_code == 404
        assert c.post("/profile/evidence", data={"profile_id": b, "kind": "x", "fact_id": fact, "quote": "x" * 20},
                      files={"file": ("a.txt", io.BytesIO(b"hello"))}).status_code == 404
        assert c.put(f"/profile/facts/{fact}", params={"profile_id": a}, json={"evidence_status": "document_on_file"}).status_code == 422, \
            "document status can only come from a checked document"


def test_upload_limits(world, monkeypatch):
    db, a, b, lot, vid, llm = world
    with TestClient(app) as c:
        post = lambda name, body: c.post(f"/lots/{lot}/documents", data={"profile_id": a}, files={"file": (name, io.BytesIO(body))})  # noqa: E731
        assert post("x.exe", b"MZ").status_code == 415
        assert post("fake.pdf", b"not a pdf at all").status_code == 415
        assert post("fake.docx", b"not a zip").status_code == 415
        monkeypatch.setattr(settings, "max_upload_bytes", 10)
        assert post("big.pdf", b"%PDF-" + b"0" * 100).status_code == 413


@pytest.mark.parametrize("url", ["http://127.0.0.1/admin", "http://localhost:8000/", "http://169.254.169.254/latest/meta-data",
                                 "http://[::1]/", "http://10.0.0.5/x", "http://192.168.1.1/", "ftp://example.com/a", "file:///etc/passwd"])
def test_links_to_non_public_addresses_are_refused(url):
    assert not fetching.is_public_url(url)
    assert fetching.fetch(url).outcome == "error"


def test_public_addresses_pass_and_redirects_are_checked_at_each_hop(monkeypatch):
    def resolve(host, *a, **k):
        ip = {"public.example": "93.184.216.34", "evil.example": "10.0.0.8"}[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert fetching.is_public_url("https://public.example/a.pdf")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/secret"})
    real = httpx.Client
    monkeypatch.setattr(fetching.httpx, "Client", lambda **k: real(transport=httpx.MockTransport(handler), **k))
    out = fetching.fetch("https://public.example/a.pdf")
    assert out.outcome == "error" and "non-public" in out.note
    assert seen == ["https://public.example/a.pdf"], "the redirect target was never contacted"


def test_redirect_loops_stop():
    n = []
    def handler(request):
        n.append(1)
        return httpx.Response(302, headers={"location": "https://portal.test/again"})
    out = fetching.fetch("https://portal.test/a", httpx.Client(transport=httpx.MockTransport(handler)))
    assert out.outcome == "error" and "too many" in out.note and len(n) == fetching.MAX_REDIRECTS + 1


