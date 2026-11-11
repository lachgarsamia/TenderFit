"""Supplier A can never reach or influence supplier B, directly or through an id nested in another request."""
import io
import socket

import httpx
import pytest

from tenderfit.assessment.pipeline import assess_lot
from tenderfit.changes.apply import apply_profile_change, process_pending
from tenderfit.changes.impact import load_deps
from tenderfit.config import settings
from tenderfit.documents import fetch as fetching
from tenderfit.documents import service
from tenderfit.ingest.importer import run_import
from tenderfit.jobs import queue
from tenderfit.profile.seed import evidence_pdf, seed
from tests import northfield
from tests.conftest import as_supplier
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


def fact_of(db, profile, attribute="insurance"):
    return db.execute("SELECT id FROM profile_fact WHERE profile_id = %s AND attribute = %s LIMIT 1", (profile, attribute)).fetchone()["id"]


def test_a_suppliers_answer_never_reaches_another_suppliers_prompt(world):
    db, a, b, lot, vid, llm = world
    service.attach_upload(db, vid, "ITT.docx", northfield.itt_docx(), "operator")
    aid = assess_lot(db, lot, a, llm, **KW)
    v = verdict_of(db, aid, "Share of recycled")
    q = db.execute("SELECT id FROM question WHERE verdict_id = %s", (v["id"],)).fetchone()
    secret = "SECRET-MARGIN-ANSWER-OF-SUPPLIER-A"
    with as_supplier(db, a) as ca, as_supplier(db, b) as cb:
        assert cb.post(f"/questions/{q['id']}/answer", json={"answer": secret}).status_code == 404, "B cannot answer for A"
        assert ca.post(f"/questions/{q['id']}/answer", json={"answer": secret}).status_code == 200
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
    with as_supplier(db, a) as ca, as_supplier(db, b) as cb:
        r = ca.post(f"/lots/{lot}/documents", data={"ref_id": itt["id"]}, files={"file": ("ITT.docx", io.BytesIO(northfield.itt_docx()))})
        assert r.status_code == 200, r.text
        assert r.json()["jobs"] == [], "A has no assessment yet, and B's is not touched"
        shown_a, shown_b = ca.get(f"/lots/{lot}/documents").json(), cb.get(f"/lots/{lot}/documents").json()
    label = "Instructions to Tenderers"
    assert next(d for d in shown_a["documents"] if d["label"] == label)["status"] == "read"
    assert next(d for d in shown_b["documents"] if d["label"] == label)["status"] == "not_retrieved"
    assert service.inventory(db, vid, b)["coverage"] == b_before["coverage"]
    assert all(d["label"] != "Appendix C" for d in shown_b["documents"]), "references found in A's file are A's"
    aid = assess_lot(db, lot, a, llm, **KW)
    assert verdict_of(db, aid, "Comparable contract"), "A's private document yields requirements for A"
    bid = assess_lot(db, lot, b, llm, **KW)
    assert not verdict_of(db, bid, "Comparable contract"), "and none for B"
    private = db.execute("SELECT owner_profile_id FROM requirement WHERE statement LIKE 'Comparable contract%%'").fetchone()
    assert private["owner_profile_id"] == a


def test_private_text_stays_out_of_retrieval_and_prompts_for_others(world):
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
    before = len(llm.users)
    assess_lot(db, lot, b, llm, **KW)
    assert all("comparable contract" not in u for u in llm.users[before:]), "no prompt built for B contains A's document"


def test_confirmations_are_scoped_and_other_suppliers_targets_look_missing(world):
    db, a, b, lot, vid, llm = world
    itt = next(d for d in service.inventory(db, vid)["documents"] if d["label"] == "Instructions to Tenderers")
    aid = assess_lot(db, lot, a, llm, **KW)
    v = verdict_of(db, aid, "Deliver all desks")
    with as_supplier(db, a) as ca, as_supplier(db, b) as cb:
        assert ca.post("/confirmations", json={"point": "G7", "target_id": itt["id"], "decision": "none_issued"}).status_code == 200
        for point, decision, target in (("G6", "override", v["id"]), ("G2", "confirm_declaration", v["id"]), ("G3", "confirm_for_lot", v["id"]),
                                        ("G5", "clarification_reviewed", v["id"]), ("G4", "reconfirmed", fact_of(db, a))):
            r = cb.post("/confirmations", json={"point": point, "target_id": target, "decision": decision, "reason": "x"})
            assert r.status_code == 404, (point, r.text)
    status = lambda p: next(d for d in service.inventory(db, vid, p)["documents"] if d["label"] == "Instructions to Tenderers")["status"]
    assert status(a) == "confirmed_none" and status(b) == "not_retrieved" and status(None) == "not_retrieved"
    assert db.execute("SELECT override_status FROM verdict WHERE id = %s", (v["id"],)).fetchone()["override_status"] is None
    assert db.execute("SELECT count(*) n FROM lot_fact_confirmation").fetchone()["n"] == 0


def test_another_suppliers_private_document_reference_looks_missing(world):
    db, a, b, lot, vid, llm = world
    ref = service.attach_upload(db, vid, "private.docx", northfield.itt_docx(), "a", None, a)["ref_id"]
    with as_supplier(db, b) as cb:
        assert cb.post("/confirmations", json={"point": "G7", "target_id": ref, "decision": "none_issued"}).status_code == 404
        up = cb.post(f"/lots/{lot}/documents", data={"ref_id": ref}, files={"file": ("x.docx", io.BytesIO(northfield.itt_docx()))})
        assert up.status_code == 404


def test_amendment_events_and_retries_are_scoped(world):
    db, a, b, lot, vid, llm = world
    assess_lot(db, lot, a, llm, **KW)
    r = northfield.release(rid="029400-2026", tag="tenderUpdate", date="2026-10-12T18:00:00Z", deadline="2099-12-31T12:00:00Z",
                           spec_url="https://portal.test/Spec_Desks_v2.pdf")
    run_import(db, fake([r]), T0, T1, now=NOW)
    process_pending(db, llm, http=portal(northfield.pdf(northfield.SPEC_V2)), **KW)
    ev = db.execute("SELECT id FROM change_event WHERE kind = 'version'").fetchone()
    db.execute("UPDATE change_event SET status = 'failed' WHERE id = %s", (ev["id"],))
    db.commit()
    with as_supplier(db, a) as ca, as_supplier(db, b) as cb:
        assert len(ca.get("/changes").json()) == 1
        assert cb.get("/changes").json() == []
        assert cb.post(f"/changes/{ev['id']}/retry").status_code == 404
        assert ca.post(f"/changes/{ev['id']}/retry").status_code == 200


def test_cross_profile_amendment_processing_keeps_each_suppliers_decisions(world):
    db, a, b, lot, vid, llm = world
    assess_lot(db, lot, a, llm, **KW)
    assess_lot(db, lot, b, llm, **KW)
    r = northfield.release(rid="029400-2026", tag="tenderUpdate", date="2026-10-12T18:00:00Z", deadline="2099-12-31T12:00:00Z",
                           spec_url="https://portal.test/Spec_Desks_v2.pdf")
    run_import(db, fake([r]), T0, T1, now=NOW)
    process_pending(db, llm, http=portal(northfield.pdf(northfield.SPEC_V2)), **KW)
    facts = {p: {x["id"] for x in db.execute("SELECT id FROM profile_fact WHERE profile_id = %s", (p,))} for p in (a, b)}
    rows = db.execute("""SELECT a.profile_id, vf.fact_id FROM verdict_fact vf JOIN verdict v ON v.id = vf.verdict_id
                         JOIN assessment a ON a.id = v.assessment_id""").fetchall()
    assert rows and all(x["fact_id"] in facts[x["profile_id"]] for x in rows), "a verdict only ever cites its own supplier's facts"


def test_a_job_can_only_touch_the_facts_of_the_supplier_it_names(world):
    db, a, b, lot, vid, llm = world
    assess_lot(db, lot, a, llm, **KW)
    aid2 = apply_profile_change(db, lot, a, [fact_of(db, b)], llm, "forged job payload", **KW)
    assert all(v["carried_over"] for v in db.execute("SELECT carried_over FROM verdict WHERE assessment_id = %s", (aid2,)))


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


def test_every_fact_and_evidence_route_refuses_another_supplier(world):
    db, a, b, lot, vid, llm = world
    fact = fact_of(db, a)
    with as_supplier(db, b) as c:
        assert c.put(f"/profile/facts/{fact}", json={"label": "hijacked"}).status_code == 404
        assert c.post(f"/profile/facts/{fact}/verify").status_code == 404
        assert c.post("/profile/evidence", data={"kind": "x", "fact_id": fact, "quote": "x" * 20},
                      files={"file": ("a.txt", io.BytesIO(b"hello"))}).status_code == 404
        assert c.post(f"/profile/facts/{fact}/renew", data={"quote": "x" * 20, "expires_on": "2099-01-01"},
                      files={"file": ("a.txt", io.BytesIO(b"hello"))}).status_code == 404
        listed = {f["id"] for f in c.get("/profile/facts").json()}
    assert fact not in listed
    assert db.execute("SELECT label FROM profile_fact WHERE id = %s", (fact,)).fetchone()["label"] != "hijacked"


def test_a_file_of_one_supplier_cannot_prove_anothers_fact(world):
    from tenderfit.profile import service as ps
    db, a, b, lot, vid, llm = world
    eid = ps.add_evidence(db, b, "insurance", "b.pdf", evidence_pdf("B", ["Cover: Public liability, limit of indemnity GBP 5,000,000."]))
    with pytest.raises(ps.ProfileError, match="different suppliers"):
        ps.link_support(db, fact_of(db, a), eid, 1, "Cover: Public liability, limit of indemnity GBP 5,000,000.")


def test_assessment_history_jobs_and_trace_are_scoped(world):
    db, a, b, lot, vid, llm = world
    aid = assess_lot(db, lot, a, llm, **KW)
    with as_supplier(db, a) as ca, as_supplier(db, b) as cb:
        jid = ca.post(f"/lots/{lot}/assess").json()["jobs"][0]
        assert ca.get(f"/jobs/{jid}").status_code == 200
        assert cb.get(f"/jobs/{jid}").status_code == 404, "another supplier's job"
        assert ca.get(f"/assessments/{aid}/trace").status_code == 200
        assert cb.get(f"/assessments/{aid}/trace").status_code == 404
        assert cb.get(f"/lots/{lot}/assessment").status_code == 404, "B has no assessment; A's is not shown"
        assert cb.get(f"/lots/{lot}/preparation").status_code == 404
    shared = queue.enqueue(db, "import", {})
    db.commit()
    with as_supplier(db, a) as ca:
        assert ca.get(f"/jobs/{shared}").status_code == 404, "shared jobs are not exposed"


def test_a_client_supplied_profile_id_is_ignored(world):
    """The old API took profile_id from the client. Sending it now changes nothing."""
    db, a, b, lot, vid, llm = world
    assess_lot(db, lot, a, llm, **KW)
    with as_supplier(db, b) as cb:
        assert cb.get(f"/lots/{lot}/assessment", params={"profile_id": a}).status_code == 404
        facts = cb.get("/profile/facts", params={"profile_id": a}).json()
        assert {f["profile_id"] for f in facts} == {b}
        assert cb.post(f"/lots/{lot}/assess", json={"profile_id": a}).status_code == 200
    job = db.execute("SELECT payload FROM job WHERE kind = 'assess' ORDER BY id DESC LIMIT 1").fetchone()
    assert job["payload"]["profile_id"] == b, "the job runs for the signed-in supplier"


def test_upload_limits(world, monkeypatch):
    db, a, b, lot, vid, llm = world
    with as_supplier(db, a) as c:
        post = lambda name, body: c.post(f"/lots/{lot}/documents", files={"file": (name, io.BytesIO(body))})
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
