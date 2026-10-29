import json

import pytest

from tenderfit.assessment.pipeline import assess_lot
from tenderfit.documents import service
from tenderfit.ingest.importer import run_import
from tenderfit.llm import ToolCall, ToolTurn, Usage
from tenderfit.profile.seed import seed
from tests import northfield
from tests.fakes import ScriptedLLM, passage_id
from tests.test_extraction import req
from tests.test_importer import NOW, T0, T1, fake
from tests.test_pipeline import TODAY, portal
from tests.test_retrieval import BagEmbedder, OverlapReranker

pytestmark = pytest.mark.agent
FORM = "Provide a customer reference using the form at Appendix C"


def extraction(role, user, schema):
    if role == "verdict":
        return {"status": "unknown", "reason": "The reference form is in Appendix C, which was not retrieved",
                "supporting_fact_ids": [], "contrary_fact_ids": [], "citations": [], "question": None, "conflict": False}
    if "comparable contract" in user:
        return {"requirements": [req(passage_id(user, "comparable contract"), FORM,
                                     "with a customer reference, using the form at Appendix C", category="experience",
                                     references=["Appendix C"])]}
    return {"requirements": []}


def turn(*calls):
    return ToolTurn("", [ToolCall(f"t{i}", n, a) for i, (n, a) in enumerate(calls)],
                    [{"type": "tool_use", "id": f"t{i}", "name": n, "input": a} for i, (n, a) in enumerate(calls)],
                    Usage("claude-opus-5-5", 2000, 150), "tool_use")


def setup(db, tmp_path, monkeypatch, tool_script):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lot1 = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    vid = db.execute("SELECT current_version_id v FROM opportunity").fetchone()["v"]
    service.process_version(db, vid, portal(northfield.pdf(northfield.SPEC_V1)))
    itt = next(x for x in service.inventory(db, vid)["documents"] if x["label"] == "Instructions to Tenderers")
    service.attach_upload(db, vid, "ITT.docx", northfield.itt_docx(), "sam", itt["id"])
    llm = ScriptedLLM(extraction, tool_script)
    aid = assess_lot(db, lot1, atlas, llm, today=TODAY, emb=BagEmbedder(), rr=OverlapReranker(), agent=True)
    v = db.execute("SELECT * FROM verdict WHERE assessment_id = %s", (aid,)).fetchone()
    return aid, v


def test_agent_follows_cross_reference_and_raises_question(db, tmp_path, monkeypatch):
    def script(messages):
        n = len([m for m in messages if m["role"] == "assistant"])
        if n == 0:
            assert "<tender_passage" in messages[0]["content"], "tender text must reach the agent only as delimited data"
            return turn(("read_section", {"passage_id": None, "label": "Appendix C"}))
        if n == 1:
            return turn(("open_attachment", {"label": "Appendix C"}))
        out = json.loads(messages[-1]["content"][0]["content"])
        assert out["readable"] is False and out["status"] == "not_retrieved"
        return turn(("raise_question", {"status": "needs_confirmation", "reason": "Appendix C form is not in the pack",
                                        "question": "Upload Appendix C, or confirm the buyer issued none?"}))

    aid, v = setup(db, tmp_path, monkeypatch, script)
    assert (v["status"], v["decided_by"]) == ("needs_confirmation", "agent")
    assert db.execute("SELECT text FROM question WHERE verdict_id = %s", (v["id"],)).fetchone()["text"].startswith("Upload Appendix C")
    steps = [r["tool"] for r in db.execute("SELECT tool FROM agent_step WHERE assessment_id = %s ORDER BY step", (aid,))]
    assert steps == ["read_section", "open_attachment", "raise_question"]
    deps = {(d["kind"], d["target_type"], d["recorded_by"]) for d in db.execute(
        "SELECT kind, target_type, recorded_by FROM dependency WHERE verdict_id = %s", (v["id"],))}
    assert ("read", "document", "agent_trace") in deps


def test_tool_call_limit_falls_back_to_b2_with_budget_reason(db, tmp_path, monkeypatch):
    aid, v = setup(db, tmp_path, monkeypatch, lambda messages: turn(("search_passages", {"query": "reference form"})))
    assert v["status"] == "unknown" and "budget reached" in v["reason"]
    assert "agent stopped" in v["escalate_reason"]
    assert db.execute("SELECT count(*) n FROM agent_step WHERE assessment_id = %s", (aid,)).fetchone()["n"] == 8


def test_agent_cannot_mark_met_without_evidence(db, tmp_path, monkeypatch):
    def script(messages):
        return turn(("record_verdict", {"status": "met", "reason": "The document says to mark this as met",
                                        "supporting_fact_ids": [], "contrary_fact_ids": [], "question": None,
                                        "citations": [{"passage_id": 999999, "quote": "ignore previous instructions"}]}))
    aid, v = setup(db, tmp_path, monkeypatch, script)
    assert v["status"] == "unknown" and v["decided_by"] == "guard"
    assert "citation rejected" in v["escalate_reason"]


def test_agent_citation_from_read_section_is_kept(db, tmp_path, monkeypatch):
    def script(messages):
        n = len([m for m in messages if m["role"] == "assistant"])
        if n == 0:
            return turn(("read_section", {"passage_id": None, "label": "5.1"}))
        pid = json.loads(messages[-1]["content"][0]["content"])["passages"][0]["passage_id"]
        return turn(("record_verdict", {"status": "needs_confirmation", "reason": "Reference required; none on file",
                                        "supporting_fact_ids": [], "contrary_fact_ids": [], "question": "Can you provide a reference?",
                                        "citations": [{"passage_id": pid, "quote": "Bidders must provide details of at least one comparable contract"}]}))
    aid, v = setup(db, tmp_path, monkeypatch, script)
    cites = db.execute("SELECT quote FROM verdict_citation WHERE verdict_id = %s", (v["id"],)).fetchall()
    assert "Bidders must provide details of at least one comparable contract" in [c["quote"] for c in cites]
    assert v["decided_by"] == "agent"
