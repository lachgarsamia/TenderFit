"""Dependencies are complete enough that targeted reassessment reaches what a full one would change; human decisions survive."""
import io
from datetime import date

import pytest
from fastapi.testclient import TestClient

from tenderfit.api.main import app
from tenderfit.assessment.confirm import ConfirmationError, confirm
from tenderfit.assessment.pipeline import assess_lot
from tenderfit.changes.apply import apply_profile_change, process_pending
from tenderfit.changes.diff import Change, FieldChange
from tenderfit.changes.impact import Dep, load_deps, plan_reassessment
from tenderfit.documents import service
from tenderfit.documents.references import find
from tenderfit.ingest.importer import run_import
from tenderfit.profile import service as profiles
from tenderfit.profile.seed import evidence_pdf, seed
from tenderfit.profile.verify import parse_expiry, verify_fact_evidence
from tests import northfield
from tests.fakes import ScriptedLLM, passage_id
from tests.test_extraction import req
from tests.test_importer import NOW, T0, T1, fake
from tests.test_pipeline import TODAY, cond, facts_in, portal, verdicts
from tests.test_retrieval import BagEmbedder, OverlapReranker

KW = dict(today=TODAY, emb=BagEmbedder(), rr=OverlapReranker(), agent=False)

SPEC = {
    1: [("2.1 Insurance", "Bidders must hold public liability insurance of at least £5,000,000 at the submission deadline.")],
    2: [("3.1 Delivery", "The supplier shall deliver all desks within 30 days of order, as set out in Appendix B.")],
    3: [("Appendix B", "Delivery windows are 08:00 to 16:00 on weekdays only. Keep this appendix with the contract.")],
    4: [("4.1 Standard", "Desks shall comply with BS EN 1729-2 as described in Appendix Z of the pack.")],
}


def scripted(role, user, schema):
    if role != "extract":
        pid = int(user.split("(passage ")[1].split(")")[0])
        return {"status": "needs_confirmation", "reason": "read the text", "supporting_fact_ids": [], "contrary_fact_ids": [],
                "citations": [{"passage_id": pid, "quote": "The supplier shall deliver all desks within 30 days of order"}]
                if "within 30 days" in user else [], "question": None, "conflict": False}
    out = []
    if "Bidders must hold public liability" in user:
        out.append(req(passage_id(user, "Bidders must hold public liability"), "Hold public liability insurance of £5,000,000",
                       "Bidders must hold public liability insurance of at least £5,000,000", category="insurance",
                       condition=cond("min_amount", "insurance.amount_gbp", bound=5_000_000)))
    if "within 30 days" in user:
        out.append(req(passage_id(user, "within 30 days"), "Deliver desks within 30 days, see Appendix B",
                       "The supplier shall deliver all desks within 30 days of order, as set out in Appendix B", category="other"))
    if "BS EN 1729-2" in user:
        out.append(req(passage_id(user, "BS EN 1729-2"), "Desks comply with BS EN 1729-2",
                       "Desks shall comply with BS EN 1729-2 as described in Appendix Z", category="standard",
                       condition=cond("has_evidence", "test_report", match="BS EN 1729-2")))
    return {"requirements": out}


def release(rid="040000-2026", date_="2026-10-01T09:00:00Z", deadline="2026-11-20T12:00:00Z", tag="tender"):
    return northfield.release(rid=rid, date=date_, deadline=deadline, tag=tag, spec_url=f"https://portal.test/{rid}.pdf")


def current(db, lot, profile):
    return db.execute("""SELECT id, version_id FROM assessment WHERE lot_id = %s AND profile_id = %s AND status = 'done'
                         AND NOT superseded AND NOT shadow""", (lot, profile)).fetchone()


def verdict(db, aid, like):
    return db.execute("""SELECT v.*, r.statement FROM verdict v JOIN requirement r ON r.id = v.requirement_id
                         WHERE v.assessment_id = %s AND r.statement LIKE %s""", (aid, like + "%")).fetchone()


@pytest.fixture
def world(db, _test_db, tmp_path, monkeypatch):
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([release()]), T0, T1, now=NOW)
    lot = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    llm = ScriptedLLM(scripted)
    aid = assess_lot(db, lot, atlas, llm, http=portal(northfield.pdf(SPEC)), **KW)
    return db, atlas, lot, llm, aid


def amend(db, llm, rid, day, deadline="2026-11-20T12:00:00Z", spec=SPEC):
    run_import(db, fake([release(rid, f"2026-10-{day}T18:00:00Z", deadline, "tenderUpdate")]), T0, T1, now=NOW)
    return process_pending(db, llm, http=portal(northfield.pdf(spec)), **KW)


def test_a_dated_fact_makes_the_verdict_depend_on_the_deadline(world):
    db, atlas, lot, llm, aid = world
    v = verdict(db, aid, "Hold public liability")
    assert v["status"] == "met"
    deps = {(d.kind, d.target_id) for d in load_deps(db, aid) if d.requirement_id == v["requirement_id"]}
    assert ("parameter", "notice.deadline") in deps


def test_deadline_moving_past_an_expiry_reopens_the_verdict(world):
    """The audit's missed dependency: this was carried as MET before."""
    db, atlas, lot, llm, aid = world
    amend(db, llm, "040100-2026", 12, deadline="2026-12-10T12:00:00Z")
    after = current(db, lot, atlas)
    v = verdict(db, after["id"], "Hold public liability")
    assert v["status"] == "needs_confirmation" and not v["carried_over"] and "before the submission deadline" in v["reason"]


def test_read_dependencies_survive_carry_over(world):
    """The audit's other missed dependency: read edges vanished after one amendment, so v3 missed a change."""
    db, atlas, lot, llm, aid = world
    edited = {**SPEC, 3: [("Appendix B", "Delivery windows are now 06:00 to 12:00 on weekdays only. Keep this appendix with the contract.")]}
    unrelated = {**SPEC, 1: [("2.1 Insurance", "Bidders must hold public liability insurance of at least £5,000,000 at the submission deadline, without exception.")]}
    amend(db, llm, "040100-2026", 12, spec=unrelated)
    v2 = current(db, lot, atlas)
    carried = verdict(db, v2["id"], "Deliver desks")
    assert carried["carried_over"], "v2 did not touch delivery"
    kinds = {d.kind for d in load_deps(db, v2["id"]) if d.requirement_id == carried["requirement_id"]}
    assert "reference" in kinds
    amend(db, llm, "040200-2026", 13, spec=edited)
    v3 = verdict(db, current(db, lot, atlas)["id"], "Deliver desks")
    assert not v3["carried_over"], "an edit to Appendix B, on the third version, reaches the requirement that cites it"


def test_a_reference_targets_every_passage_of_the_section_and_stays_in_its_document(db, tmp_path, monkeypatch):
    from tenderfit.extraction.extract import _reference_targets
    sections = {1: {"appendix b": [10, 11, 12], "1.1 intro": [13], "10.2 other": [14]}, 2: {"appendix b": [20]}}
    assert _reference_targets("Appendix B", {"document_id": 1}, sections) == [10, 11, 12]
    assert _reference_targets("Appendix B", {"document_id": 3}, sections) == [10, 11, 12, 20]
    assert _reference_targets("Section 1", {"document_id": 1}, {1: {"1.1 intro": [13], "10.2 other": [14]}}) == [13], "never 10.2"
    assert _reference_targets("Section 1.1", {"document_id": 1}, {1: {"1.1 intro": [13], "10.2 other": [14]}}) == [13]


def test_a_document_change_reaches_requirements_that_cite_it():
    plan = plan_reassessment(Change(documents=[{"kind": "added", "label": "Appendix C", "essential": False}]),
                             [Dep(100, "reference", "document", "12")], documents={12})
    assert plan.reassess == {100}


def test_reference_regex_no_longer_invents_documents():
    assert [r.label for r in find("Complete the schedule of rates and the attachment to this notice.")] == []
    assert [r.label for r in find("See Schedule 2, Appendix B and annex C.")] == ["Schedule 2", "Appendix B", "Annex C"]


def test_a_requirement_citing_a_missing_document_is_not_met_on_what_was_read(world):
    db, atlas, lot, llm, aid = world
    db.execute("UPDATE profile_fact SET evidence_status = 'document_on_file', value = %s WHERE attribute = 'test_report' AND profile_id = %s",
               ('{"standard": "BS EN 1729-2"}', atlas))
    db.commit()
    a2 = assess_lot(db, lot, atlas, llm, **KW)
    v = verdict(db, a2, "Desks comply")
    assert v["status"] == "needs_confirmation" and v["reference_gaps"] == ["Appendix Z"]
    assert "could not be read" in v["reason"] and v["evidence_quality"] == "ambiguous"


# Graph retrieval -----------------------------------------------------------------------------------------------------

def test_graph_retrieval_follows_the_reference_and_says_why(world):
    db, atlas, lot, llm, aid = world
    v = verdict(db, aid, "Deliver desks")
    why = {r["passage_id"]: r["why"] for r in v["retrieval"]}
    appendix = db.execute("SELECT id FROM passage WHERE section = 'Appendix B'").fetchone()["id"]
    assert appendix in why and any("refers to Appendix B" in w for w in why[appendix])
    assert "08:00 to 16:00" in llm.users[-1] or any("08:00 to 16:00" in u for u in llm.users), "the model read the appendix"


def test_graph_expansion_only_follows_links_the_requirement_names(world):
    from tenderfit.retrieval import graph
    db, atlas, lot, llm, aid = world
    src = db.execute("SELECT passage_id FROM requirement WHERE statement LIKE 'Deliver desks%%'").fetchone()["passage_id"]
    assert graph.expand(db, src, set(), atlas) == []
    assert graph.expand(db, src, {"Appendix Q"}, atlas) == []
    assert [e["label"] for e in graph.expand(db, src, {"appendix b"}, atlas)] == ["Appendix B"]


def test_graph_does_not_cross_supplier_boundaries(world):
    from tenderfit.retrieval import graph
    db, atlas, lot, llm, aid = world
    other = db.execute("SELECT id FROM business_profile WHERE name = 'Pennine Seating'").fetchone()["id"]
    vid = current(db, lot, atlas)["version_id"]
    private = {1: [("Appendix Q", "Atlas's confidential delivery schedule. Reference Q details here for the private pack.")],
               2: [("5.1 Notes", "See Appendix Q for delivery notes and other private arrangements in this pack. This clause forms part of the tender pack and must be read together with the Instructions to Tenderers issued by the buyer for this procurement.")]}
    service.attach_upload(db, vid, "private.pdf", northfield.pdf(private), "a", None, atlas)
    graph.build_graph(db, vid)
    src = db.execute("SELECT p.id FROM passage p WHERE p.section = '5.1 Notes'").fetchone()["id"]
    assert graph.expand(db, src, {"Appendix Q"}, atlas)
    assert graph.expand(db, src, {"Appendix Q"}, other) == [] and graph.expand(db, src, {"Appendix Q"}, None) == []


def test_contradiction_between_documents_needs_the_buyer(world):
    db, atlas, lot, llm, aid = world
    vid = current(db, lot, atlas)["version_id"]
    itt = {1: [("7.1 Delivery terms", "The supplier shall deliver all desks within 45 days of order, as set out in Appendix B. This clause forms part of the tender pack and must be read together with the Instructions to Tenderers issued by the buyer for this procurement.")]}
    db.execute("UPDATE requirement SET quote = 'The supplier shall deliver all desks within 30 days of order, as set out in Appendix B' WHERE statement LIKE 'Deliver desks%%'")
    service.attach_upload(db, vid, "ITT-terms.pdf", northfield.pdf(itt), "op")
    a2 = assess_lot(db, lot, atlas, ScriptedLLM(scripted), **KW)
    v = verdict(db, a2, "Deliver desks")
    assert v["status"] == "needs_confirmation" and v["needs_human"] == "G5" and "documents state this differently" in v["reason"]
    assert v["evidence_quality"] == "ambiguous" and "45 days" in v["question"]
    cites = {c["quote"] for c in db.execute("SELECT quote FROM verdict_citation WHERE verdict_id = %s", (v["id"],))}
    assert any("45 days" in q for q in cites)


def test_decomposed_requirement_respects_mandatory_logic(db, tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    pennine = seed(db)["Pennine Seating"]
    spec = {1: [("6.1 Quality", "Suppliers must hold ISO 9001 certification, valid throughout the contract period, issued by an accredited certification body. This clause forms part of the tender pack and must be read together with the Instructions to Tenderers issued by the buyer for this procurement.")]}
    quote = "Suppliers must hold ISO 9001 certification, valid throughout the contract period, issued by an accredited certification body."
    seen = []

    def script(role, user, schema):
        if role == "extract":
            if "Suppliers must hold ISO 9001" not in user:
                return {"requirements": []}
            pid = passage_id(user, "Suppliers must hold ISO 9001")
            return {"requirements": [req(pid, "Hold ISO 9001, valid for the contract, from an accredited body", quote, category="certificate", conditions=[
                {"label": "Required standard", "mandatory": True, "quote": "must hold ISO 9001 certification", "condition": cond("has_evidence", "certificate", match="ISO 9001"), "evidence_needed": ["certificate"]},
                {"label": "Valid throughout the contract", "mandatory": True, "quote": "ISO 9001 certification, valid throughout the contract period", "condition": cond("valid_through", "certificate", match="ISO 9001", anchor="lot.contract_end"), "evidence_needed": []},
                {"label": "Issued by an accredited body", "mandatory": True, "quote": "issued by an accredited certification body", "condition": None, "evidence_needed": []},
            ])]}
        seen.append(user)
        fact = [f["id"] for f in facts_in(user) if f["attribute"] == "certificate"]
        return {"status": "met", "reason": "The certificate names an accredited body", "supporting_fact_ids": fact, "contrary_fact_ids": [],
                "citations": [], "question": None, "conflict": False}
    r = northfield.release()
    r["tender"]["lots"][0]["contractPeriod"] = {"endDate": "2029-03-31"}
    run_import(db, fake([r]), T0, T1, now=NOW)
    lot = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    aid = assess_lot(db, lot, pennine, ScriptedLLM(script), http=portal(northfield.pdf(spec)), **KW)
    v = verdict(db, aid, "Hold ISO 9001")
    rows = {r["label"]: r for r in db.execute("""SELECT rc.label, vc.status, vc.decided_by FROM verdict_condition vc
        JOIN requirement_condition rc ON rc.id = vc.condition_id WHERE vc.verdict_id = %s""", (v["id"],))}
    assert rows["Required standard"]["decided_by"] == "rule" and rows["Required standard"]["status"] == "met"
    assert rows["Valid throughout the contract"]["status"] == "needs_confirmation", "the fictional certificate ends in 2027"
    assert rows["Issued by an accredited body"]["decided_by"] == "model"
    assert "Issued by an accredited body" in seen[0] and "Required standard" not in seen[0], "only open parts go to the model"
    assert v["status"] == "needs_confirmation", "the model's MET cannot lift a condition a rule did not accept"


# Human decisions ------------------------------------------------------------------------------------------------------

def test_an_override_survives_several_amendments_and_stays_visible(db, tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lot = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    llm = ScriptedLLM(verdicts)
    aid = assess_lot(db, lot, atlas, llm, http=portal(northfield.pdf(northfield.SPEC_V1)), **KW)
    v = verdict(db, aid, "Deliver all desks")
    assert v["status"] == "not_met"
    confirm(db, "G6", "sam", v["id"], "override", "Buyer agreed by phone", profile_id=atlas)
    a2 = assess_lot(db, lot, atlas, llm, **KW)
    for rid, day in (("029400-2026", 12), ("029500-2026", 13), ("029600-2026", 14)):
        r = northfield.release(rid=rid, tag="tenderUpdate", date=f"2026-10-{day}T18:00:00Z", spec_url="https://portal.test/same.pdf")
        r["tender"]["description"] = f"Teaching-space furniture, notice {rid}."
        run_import(db, fake([r]), T0, T1, now=NOW)
        process_pending(db, llm, http=portal(northfield.pdf(northfield.SPEC_V1)), **KW)
    now = current(db, lot, atlas)
    assert now["id"] != a2
    kept = verdict(db, now["id"], "Deliver all desks")
    assert kept["override_status"] == "needs_confirmation" and kept["override_by"] == "sam" and kept["override_reason"] == "Buyer agreed by phone"
    rec = db.execute("SELECT recommendation FROM assessment WHERE id = %s", (now["id"],)).fetchone()["recommendation"]
    assert not rec.startswith("Blocker")


def test_an_override_does_not_carry_when_the_wording_changed(db, tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lot = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    llm = ScriptedLLM(verdicts)
    aid = assess_lot(db, lot, atlas, llm, http=portal(northfield.pdf(northfield.SPEC_V1)), **KW)
    confirm(db, "G6", "sam", verdict(db, aid, "Deliver all desks")["id"], "override", "ok", profile_id=atlas)
    spec = {**northfield.SPEC_V1, 6: [("3.2 Delivery", "The supplier shall deliver all desks within 10 days of order. Delivery shall be made "
                                       "between 08:00 and 16:00 on Working Days as defined in Appendix B.")]}

    def script(role, user, schema):
        if role == "extract" and "within 10 days" in user:
            return {"requirements": [req(passage_id(user, "within 10 days"), "Deliver all desks within 10 days of order",
                                         "The supplier shall deliver all desks within 10 days of order.",
                                         condition=cond("at_most", "capacity.lead_days", bound=10))]}
        return verdicts(role, user, schema)
    r = northfield.release(rid="029400-2026", tag="tenderUpdate", date="2026-10-12T18:00:00Z", spec_url="https://portal.test/s2.pdf")
    run_import(db, fake([r]), T0, T1, now=NOW)
    process_pending(db, ScriptedLLM(script), http=portal(northfield.pdf(spec)), **KW)
    v = verdict(db, current(db, lot, atlas)["id"], "Deliver all desks within 10")
    assert v["status"] == "not_met" and v["override_status"] is None, "a stricter requirement is a new decision"


def test_a_workload_confirmation_holds_for_one_version_only(db, tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    v2 = northfield.release(rid="029400-2026", date="2026-10-12T18:00:00Z", spec_url="https://portal.test/s.pdf")
    run_import(db, fake([v2]), T0, T1, now=NOW)
    lot = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    llm = ScriptedLLM(verdicts)
    aid = assess_lot(db, lot, atlas, llm, http=portal(northfield.pdf(northfield.SPEC_V2)), **KW)
    v = verdict(db, aid, "Deliver all desks")
    assert v["needs_human"] == "G3"
    confirm(db, "G3", "sam", v["id"], "confirm_for_lot", profile_id=atlas)
    assert verdict(db, assess_lot(db, lot, atlas, llm, **KW), "Deliver all desks")["status"] == "met"
    new = northfield.release(rid="029500-2026", tag="tenderUpdate", date="2026-10-13T18:00:00Z", spec_url="https://portal.test/s.pdf")
    run_import(db, fake([new]), T0, T1, now=NOW)
    vid = db.execute("SELECT current_version_id v FROM opportunity").fetchone()["v"]
    assert verdict(db, assess_lot(db, lot, atlas, llm, version_id=vid, http=portal(northfield.pdf(northfield.SPEC_V2)), **KW),
                   "Deliver all desks")["needs_human"] == "G3", "the new version asks again"


def test_answering_a_question_ends_the_loop_without_proving_anything(world):
    db, atlas, lot, llm, aid = world
    v = verdict(db, aid, "Desks comply")
    assert v["status"] == "unknown" and v["question"]
    q = db.execute("SELECT id FROM question WHERE verdict_id = %s", (v["id"],)).fetchone()
    db.execute("UPDATE question SET answer = 'Yes, we hold one', answered_at = now() WHERE id = %s", (q["id"],))
    db.commit()
    again = verdict(db, assess_lot(db, lot, atlas, llm, **KW), "Desks comply")
    assert again["status"] == "needs_confirmation" and again["question"] is None and "declaration" in again["reason"]


# Evidence ------------------------------------------------------------------------------------------------------------

def fact_row(db, atlas, attribute):
    return db.execute("SELECT * FROM profile_fact WHERE profile_id = %s AND attribute = %s ORDER BY id LIMIT 1", (atlas, attribute)).fetchone()


def test_expired_fact_can_be_renewed_and_triggers_reassessment(world):
    db, atlas, lot, llm, aid = world
    ins = fact_row(db, atlas, "insurance")
    db.execute("UPDATE profile_fact SET valid_until = %s, evidence_status = 'needs_update', needs_update_reason = 'expired' WHERE id = %s",
               (date(2020, 1, 1), ins["id"]))
    db.commit()
    new_policy = evidence_pdf("Insurance schedule (fictional)", ["Insured: Atlas Furniture Ltd.", "Cover: Public liability, limit of indemnity GBP 5,000,000.",
                                                                "Period of insurance: 1 December 2029 to 30 November 2030."])
    with TestClient(app) as c:
        r = c.post(f"/profile/facts/{ins['id']}/renew",
                   data={"profile_id": atlas, "quote": "Cover: Public liability, limit of indemnity GBP 5,000,000.", "page": 1, "expires_on": "2030-11-30"},
                   files={"file": ("renewal.pdf", io.BytesIO(new_policy))})
        assert r.status_code == 200 and r.json()["verified"], r.text
    f = fact_row(db, atlas, "insurance")
    assert f["evidence_status"] == "document_on_file" and f["valid_until"] == date(2030, 11, 30) and f["needs_update_reason"] is None


def test_reconfirming_an_expired_fact_is_refused(world):
    db, atlas, lot, llm, aid = world
    ins = fact_row(db, atlas, "insurance")
    db.execute("UPDATE profile_fact SET valid_until = %s, evidence_status = 'needs_update' WHERE id = %s", (date(2020, 1, 1), ins["id"]))
    db.commit()
    with pytest.raises(ConfirmationError, match="expired"):
        confirm(db, "G4", "sam", ins["id"], "reconfirmed", profile_id=atlas)


def test_reconfirming_never_downgrades_a_document_backed_fact(world):
    db, atlas, lot, llm, aid = world
    ins = fact_row(db, atlas, "insurance")
    db.execute("UPDATE profile_fact SET valid_until = %s, evidence_status = 'needs_update' WHERE id = %s", (date(2030, 1, 1), ins["id"]))
    db.commit()
    confirm(db, "G4", "sam", ins["id"], "reconfirmed", profile_id=atlas)
    assert fact_row(db, atlas, "insurance")["evidence_status"] == "document_on_file"


FACT = {"attribute": "certificate", "label": "ISO 9001", "value": {"standard": "ISO 9001"}, "valid_until": None}
TODAY_REAL = date(2026, 11, 3)


def test_verification_levels_report_what_was_actually_checked():
    good = ["Atlas Furniture Ltd holds ISO 9001:2015 certification. Valid until 30 June 2028."]
    r = verify_fact_evidence(FACT, "Atlas Furniture", good, True, TODAY_REAL, None)
    assert r["level"] == "current" and r["expiry"] == date(2028, 6, 30) and r["checks"]["supplier"]["result"] == "ok"
    expired = verify_fact_evidence(FACT, "Atlas Furniture", ["Atlas holds ISO 9001. Valid until 30 June 2020."], True, TODAY_REAL, None)
    assert expired["level"] == "supports" and expired["checks"]["current"]["result"] == "mismatch"
    wrong = verify_fact_evidence(FACT, "Atlas Furniture", ["This certifies ISO 14001 only."], True, TODAY_REAL, None)
    assert wrong["mismatch"] and wrong["level"] == "relevant"
    unread = verify_fact_evidence(FACT, "Atlas Furniture", ["  "], False, TODAY_REAL, None)
    assert unread["level"] == "exists" and unread["checks"]["contents"]["result"] == "not_inspected"
    entered = verify_fact_evidence(FACT, "Atlas Furniture", good, True, TODAY_REAL, date(2031, 1, 1))
    assert entered["mismatch"] and "not 2031-01-01" in entered["checks"]["expiry"]["detail"]


def test_a_file_that_contradicts_the_fact_does_not_make_it_document_backed(world):
    db, atlas, lot, llm, aid = world
    iso = fact_row(db, atlas, "certificate")
    pdf = evidence_pdf("Certificate", ["Atlas Furniture Ltd holds ISO 14001 environmental certification, renewed annually."])
    eid = profiles.add_evidence(db, atlas, "certificate", "wrong.pdf", pdf)
    assert not profiles.link_support(db, iso["id"], eid, 1, "Atlas Furniture Ltd holds ISO 14001 environmental certification")
    assert fact_row(db, atlas, "certificate")["evidence_status"] == "unknown"
    v = db.execute("SELECT level, checks FROM fact_verification WHERE fact_id = %s ORDER BY id DESC LIMIT 1", (iso["id"],)).fetchone()
    assert v["checks"]["standard"]["result"] == "mismatch"


def test_human_verification_is_separate_and_explicit(world):
    db, atlas, lot, llm, aid = world
    ins = fact_row(db, atlas, "insurance")
    assert all(s["human_verified_by"] is None for s in db.execute("SELECT human_verified_by FROM fact_support WHERE fact_id = %s", (ins["id"],)))
    with TestClient(app) as c:
        assert c.post(f"/profile/facts/{ins['id']}/verify", json={"profile_id": atlas, "user": "sam"}).status_code == 200
        facts = {f["id"]: f for f in c.get("/profile/facts", params={"profile_id": atlas}).json()}
    assert facts[ins["id"]]["supports"][0]["human_verified_by"] == "sam"
    assert facts[ins["id"]]["verification"]["level"] in ("supports", "current")


def test_parse_expiry_formats():
    assert parse_expiry("Period of insurance: 1 December 2025 to 30 November 2026.") == date(2026, 11, 30)
    assert parse_expiry("expires on 2027-05-31") == date(2027, 5, 31)
    assert parse_expiry("No dates here") is None


def test_profile_change_path_still_targets_only_used_facts(world):
    db, atlas, lot, llm, aid = world
    ins = fact_row(db, atlas, "insurance")
    new = apply_profile_change(db, lot, atlas, [ins["id"]], llm, "policy updated", **KW)
    assert verdict(db, new, "Hold public liability")["carried_over"] is False
    assert verdict(db, new, "Deliver desks")["carried_over"] is True


def test_plan_covers_field_changes():
    plan = plan_reassessment(Change(fields=[FieldChange("notice.deadline", "a", "b")]), [Dep(1, "parameter", "field", "notice.deadline")])
    assert plan.rules_only == {1}


def test_agent_evidence_tool_is_limited_to_this_suppliers_facts(world):
    from tenderfit.agent.loop import TOOLS, Tools
    db, atlas, lot, llm, aid = world
    from tenderfit.assessment.pipeline import load_facts, lot_context
    _, ctx = lot_context(db, lot, TODAY)
    facts = load_facts(db, atlas, lot)
    tools = Tools(db, current(db, lot, atlas)["version_id"], facts, ctx, BagEmbedder(), OverlapReranker(), atlas)
    ins = fact_row(db, atlas, "insurance")
    assert tools.check_evidence(ins["id"])["verifications"][0]["level"] == "current"
    other = db.execute("SELECT f.id FROM profile_fact f JOIN business_profile p ON p.id = f.profile_id WHERE p.name <> 'Atlas Furniture' LIMIT 1").fetchone()
    assert "error" in tools.check_evidence(other["id"])
    assert {t["name"] for t in TOOLS} >= {"check_evidence", "record_verdict"} and not any("url" in str(t["input_schema"]).lower() for t in TOOLS)
