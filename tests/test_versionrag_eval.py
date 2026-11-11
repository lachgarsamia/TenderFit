"""Real-notice date and insurance probes, with a separate Northfield synthetic control."""
import json
import re
from datetime import date
from pathlib import Path

import pytest

from tenderfit.assessment.pipeline import assess_lot, lot_context
from tenderfit.changes import apply
from tenderfit.config import settings
from tenderfit.demo import __main__ as demo
from tenderfit.demo.llm import DemoLLM
from tenderfit.documents import service
from tenderfit.eval.run import _import
from tenderfit.retrieval.search import index_version, search
from tests.test_versionrag import ARMS, MODELS

ROOT = Path(__file__).parents[1] / "eval/real/fts-2023"


def release(original_id, record, release_id, deadline, original=True):
    return {"ocid": "pilot-" + original_id, "id": release_id, "date": record["dispatch"] + "T12:00:00Z",
            "tag": ["tender" if original else "tenderUpdate"],
            "tender": {"title": record["title"], "description": record["sector"],
                       "lots": [{"id": "1", "title": record["title"]}],
                       "tenderPeriod": {"endDate": deadline}}}


def test_real_deadline_snapshots(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "document_dir", str(tmp_path))
    sources = json.loads((ROOT / "sources.json").read_text())
    cases = json.loads((ROOT / "deadline_labels.json").read_text())
    totals = {name: dict(retrieved=0, correct=0, false_met=0, changed=0) for name in ["hybrid", "VersionRAG"]}
    for case in cases:
        original, amendment = case["original"], case["amendment"]
        before, after = sources[original], sources[amendment]
        fields = next(c for c in after["changes"] if "IV.2.2" in c["where"] and re.match(r"\d{4}-", c["new"]))
        assert fields["old"].split()[0] == before["submission"] == case["old"]
        dates = [before["submission"], fields["new"].split()[0]]
        for i, (rid, row) in enumerate([(original, before), (amendment, after)]):
            _import(db, release(original, row, rid, dates[i], original=i == 0))
        versions = db.execute("SELECT id,fields FROM notice_version WHERE ocid=%s ORDER BY published_at,id",
                              ("pilot-" + original,)).fetchall()
        lot = db.execute("SELECT l.id FROM lot l JOIN opportunity o ON o.id=l.opportunity_id WHERE o.ocid=%s",
                         ("pilot-" + original,)).fetchone()["id"]
        from tenderfit.changes.diff import field_diff
        change = next(c for c in field_diff(versions[0]["fields"], versions[1]["fields"]) if c.path == "notice.deadline")
        for name, counts in totals.items():
            counts["changed"] += (change.before[:10], change.after[:10]) == (case["old"], case["new"])
            for index, v in enumerate(versions):
                service.process_version(db, v["id"])
                index_version(db, v["id"], MODELS["emb"])
                hits = search(db, v["id"], "Submission deadline", k=1, **MODELS)
                counts["retrieved"] += hits[0].section == "Notice: submission" and dates[index] in hits[0].text
                # Both arms retrieve versioned passages. Only the new assessment pins the structured read too.
                _, ctx = lot_context(db, lot, date(2023, 1, 1), v["id"] if name == "VersionRAG" else None)
                prediction = "met" if case["supplier_ready"] <= ctx.deadline.isoformat() else "not_met"
                counts["correct"] += prediction == case["expected"][index]
                counts["false_met"] += prediction == "met" and case["expected"][index] != "met"
    print("REAL date-only probe", json.dumps(totals))
    assert len(cases) == 20 and totals["VersionRAG"]["correct"] == 40


@pytest.mark.parametrize("cfg", ARMS, ids=lambda cfg: cfg.name)
def test_real_questionnaire(db, tmp_path, monkeypatch, cfg):
    """Replay one scoped insurance component using the F14's published old and new wording."""
    from tenderfit.demo.llm import _passages, c, r
    from tenderfit.documents.text import quote_ok
    from tenderfit.llm import JsonResult, Usage
    from tenderfit.profile import service as profiles

    class PolicyExtract(DemoLLM):
        def json(self, role, system, user, schema):
            if role != "extract":
                return super().json(role, system, user, schema)
            requirements = []
            for pid, text, _ in _passages(user):
                match = re.search(r"Public Liability Insurance = £([\d,]+)\.00", text)
                if match:
                    amount = int(match[1].replace(",", ""))
                    requirements.append(r(pid, None, match[0], match[0], "insurance",
                                          condition=c("min_amount", "insurance.amount_gbp", bound=amount, qualifier="public liability")))
            return JsonResult({"requirements": requirements}, Usage("fixed-questionnaire-parser", 0, 0))

    monkeypatch.setattr(settings, "document_dir", str(tmp_path))
    class EvidenceDate(date):
        @classmethod
        def today(cls):
            return cls(2023, 10, 23)

    monkeypatch.setattr(profiles, "date", EvidenceDate)
    sources = json.loads((ROOT / "sources.json").read_text())
    gold = json.loads((ROOT / "insurance_label.json").read_text())
    orig, correction = gold["original"], gold["amendment"]
    buyer_edit = sources[correction]["changes"][0]
    profile = profiles.create_profile(db, "Pilot Builder", [], "Cannot increase its public liability insurance")
    demo.add_supported(db, profile, "insurance", "Public liability", {"type": "public_liability", "amount_gbp": 5_000_000},
                       "Insurance", ["Pilot Builder, public liability cover GBP 5,000,000."],
                       "public liability cover GBP 5,000,000", valid_until=date(2025, 1, 1))
    db.commit()
    runs = []
    for i, rid in enumerate((orig, correction)):
        record = release(orig, sources[rid], rid, sources[orig]["submission"], original=i == 0)
        record["tender"]["selectionCriteria"] = {"criteria": [{"description": buyer_edit[("old", "new")[i]]}]}
        _import(db, record)
        lot = db.execute("SELECT id FROM lot").fetchone()["id"]
        kw = dict(config=cfg, today=date(2023, 10, 23), **MODELS)
        if i == 0:
            runs.append(assess_lot(db, lot, profile, PolicyExtract(), **kw))
        else:
            [impact] = apply.process_pending(db, PolicyExtract(), **kw)
            row = db.execute("SELECT * FROM change_impact WHERE id=%s", (impact,)).fetchone()
            runs.append(row["after_assessment_id"])
            print("REAL Birmingham", cfg.name, row["mode"], row["explanation"])
    for i, run in enumerate(runs):
        [outcome] = db.execute("""SELECT v.id,v.status,v.required,r.statement FROM verdict v JOIN requirement r ON r.id=v.requirement_id
                                WHERE v.assessment_id=%s""", (run,)).fetchall()
        assert outcome["status"] == gold["expected"][i]
        bound = gold[("old_gbp", "new_gbp")[i]]
        assert f"{bound:,}" in outcome["statement"]
        cites = db.execute("""SELECT c.quote,p.text,d.version_id=a.version_id AS current_source FROM verdict_citation c
            JOIN passage p ON p.id=c.passage_id JOIN document d ON d.id=p.document_id
            JOIN verdict v ON v.id=c.verdict_id JOIN assessment a ON a.id=v.assessment_id WHERE c.verdict_id=%s""", (outcome["id"],)).fetchall()
        assert cites and all(c["current_source"] and quote_ok(c["quote"], c["text"]) for c in cites)


@pytest.mark.parametrize("cfg", ARMS, ids=lambda cfg: cfg.name)
def test_northfield_metrics(_test_db, tmp_path, monkeypatch, cfg):
    from tenderfit.eval.run import run_config, summarise
    from tests.fakes import ScriptedLLM
    from tests.test_pipeline import verdicts

    monkeypatch.setattr(settings, "database_url", _test_db)
    monkeypatch.setattr("tenderfit.eval.run.EVAL_DB", "tenderfit_eval_pilot_test")
    results = run_config(Path("eval/datasets/northfield"), cfg, ScriptedLLM(verdicts), date(2026, 10, 14), **MODELS)
    summary = summarise(results)
    metrics = ("extraction_recall_mandatory", "extraction_precision", "macro_f1", "false_met_rate",
               "quote_match_rate", "affected_precision", "affected_recall", "amend_verdict_macro_f1")
    print("SYNTHETIC Northfield", cfg.name, json.dumps({k: summary[k] for k in metrics}))
    assert results and summary["quote_match_rate"] == 1


def test_one_hop_read_set_ablation(db, tmp_path, monkeypatch):
    """Retain the new version/provenance/carry policy while omitting second-hop search candidates."""
    from tenderfit.assessment.config import PipelineConfig
    from tenderfit.retrieval import versioned
    from tests.test_versionrag import timetable_trial

    both_hops = versioned.resolve
    def direct_links(*args, **kwargs):
        result = both_hops(*args, **kwargs)
        result.links = [link for link in result.links if len(link["path"]) == 2]
        return result
    monkeypatch.setattr(versioned, "resolve", direct_links)
    timetable_trial(db, tmp_path, monkeypatch, PipelineConfig("one-hop-read-set", versionrag=True))
