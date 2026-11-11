"""Reproduce the pilot with scripts/test.sh tests/test_versionrag.py -s on a disposable database."""
import os
from datetime import date

import pytest

from tenderfit.assessment.config import BASELINES, PipelineConfig
from tenderfit.assessment.pipeline import assess_lot
from tenderfit.changes import apply
from tenderfit.config import settings
from tenderfit.demo import __main__ as demo
from tenderfit.demo import scenario
from tenderfit.demo.llm import DemoLLM
from tenderfit.eval.run import _import
from tenderfit.retrieval.search import embedder, reranker

MODELS = {"emb": demo.HashEmbedder(), "rr": demo.OverlapReranker()}
if os.getenv("VERSIONRAG_ENCODERS") == "production":
    MODELS = {"emb": embedder(), "rr": reranker()}
ARMS = [BASELINES["B1"], PipelineConfig("one-hop"), BASELINES["VersionRAG"]]


@pytest.mark.parametrize("cfg", ARMS, ids=lambda cfg: cfg.name)
def test_two_hop_amendment(db, tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(settings, "document_dir", str(tmp_path))
    monkeypatch.setattr(settings, "demo_scripted_llm", True)
    today = date(2026, 11, 11)
    pid = demo.seed_suppliers(db, today)[demo.SUPPLIERS[0]]
    _import(db, scenario.release(today, spec="https://portal.test/harrowgate/Timetable_v1.pdf"))
    lot = db.execute("SELECT id FROM lot WHERE lot_no='1'").fetchone()["id"]
    kw = dict(config=cfg, today=today, **MODELS)
    aid = assess_lot(db, lot, pid, DemoLLM(), http=demo.portal(), **kw)
    _import(db, scenario.release(today, rid=demo.AMENDMENT, tag="tenderUpdate", published_days_ago=1, deadline_in=35,
                                 spec="https://portal.test/harrowgate/Timetable_v2.pdf"))
    changed = demo.portal()
    impacts = apply.process_pending(db, DemoLLM(), http=changed, **kw)
    impact = db.execute("SELECT * FROM change_impact WHERE id=%s", (impacts[0],)).fetchone()
    print(cfg.name, impact["explanation"])
    states = []
    retrieved = relevant_hits = 0
    for run in [aid, impact["after_assessment_id"]]:
        row = db.execute("""SELECT v.*,a.version_id FROM verdict v JOIN requirement r ON r.id=v.requirement_id
            JOIN assessment a ON a.id=v.assessment_id WHERE a.id=%s AND r.statement='Install all desks on Working Days'""", (run,)).fetchone()
        relevant = {"Appendix B Definitions", "Schedule C Timetable"}
        sections = {r.get("section") for r in row["retrieval"] if r.get("section")}
        print(cfg.name, "release", row["version_id"], "install", row["status"], "evidence", sorted(sections),
              "paths", [r["path"] for r in row["retrieval"] if "path" in r])
        unique = {e["passage_id"]: e.get("section") for e in row["retrieval"] if e.get("section")}
        retrieved += len(unique)
        relevant_hits += sum(s in relevant for s in unique.values())
        states.append(row["status"])
        if cfg.versionrag:
            assert relevant <= sections
            assert any(len(r.get("path", [])) == 3 for r in row["retrieval"])
            if run != aid:
                assert any(r["prior_passage_id"] for r in row["retrieval"] if r["section"] == "Schedule C Timetable")
            assert all(r["version_id"] == row["version_id"] for r in row["retrieval"])
    expected_changes = {"Hold public liability insurance of at least £5,000,000", "Install all desks on Working Days"}
    reopened = {r["statement"] for r in impact["affected"]}
    print("SYNTHETIC metrics", cfg.name, dict(dependency_hits=relevant_hits, dependency_gold=4, retrieved=retrieved,
          correct=sum(a == b for a, b in zip(states, ["met", "needs_confirmation"])), false_met=states[1] == "met",
          reopened=len(reopened), useful=len(expected_changes & reopened), changed_gold=len(expected_changes)))
    if cfg.versionrag:
        assert states == ["met", "needs_confirmation"]
        assert impact["mode"] == "targeted"
    shadow = assess_lot(db, lot, pid, DemoLLM(), http=changed, shadow=True, **kw)
    def statuses(run):
        return {r["requirement_id"]: r["status"] for r in db.execute("SELECT requirement_id,status FROM verdict WHERE assessment_id=%s", (run,))}
    print(cfg.name, "complete-set agreement", statuses(shadow) == statuses(impact["after_assessment_id"]))
    if cfg.versionrag:
        assert statuses(shadow) == statuses(impact["after_assessment_id"])


def test_paths_filter_supplier_and_report_ambiguous_evidence(db):
    from tenderfit.profile.service import create_profile
    from tenderfit.retrieval import graph, versioned
    from tests.test_document_service import _version

    vid = _version(db)
    other = create_profile(db, "Private supplier", [], "")
    def passage(section, text, owner=None):
        doc = db.execute("""INSERT INTO document(version_id,origin,access,owner_profile_id)
                         VALUES (%s,'source','read',%s) RETURNING id""", (vid, owner)).fetchone()["id"]
        return db.execute("""INSERT INTO passage(document_id,section,text,sha256)
                          VALUES (%s,%s,%s,md5(%s)) RETURNING id""", (doc, section, text, text)).fetchone()["id"]
    source = passage("1 Eligibility", "Comply with Appendix B.")
    middle = passage("Appendix B Rules", "Use Schedule C.")
    private = passage("Schedule C Hours", "Secret terms from another supplier.", other)
    graph.build_graph(db, vid)
    hidden = versioned.resolve(db, vid, source, {"Appendix B"}, None)
    assert hidden.gaps == ["schedule c (unresolved)"]
    assert [link["id"] for link in hidden.links] == [middle]
    assert private in [link["id"] for link in versioned.resolve(db, vid, source, {"Appendix B"}, other).links]
    public = passage("Schedule C Hours", "Published terms.")
    graph.build_graph(db, vid)
    visible = versioned.resolve(db, vid, source, {"Appendix B"}, None)
    assert visible.links[-1]["path"] == [source, middle, public] and not visible.gaps
    ambiguous = versioned.resolve(db, vid, source, {"Appendix B"}, other)
    assert ambiguous.gaps == ["schedule c (ambiguous)"]
    assert versioned.resolve(db, vid + 1000, source, {"Appendix B"}, other).links == []


