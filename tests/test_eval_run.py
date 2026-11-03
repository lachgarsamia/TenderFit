from datetime import date
from pathlib import Path

import pytest

from tenderfit.assessment.config import BASELINES
from tenderfit.eval.run import decisions, report, run_config, summarise
from tenderfit.llm import RecordedLLM, ToolCall, ToolTurn, Usage
from tests.fakes import ScriptedLLM
from tests.test_pipeline import verdicts
from tests.test_retrieval import BagEmbedder, OverlapReranker

DATASET = Path("eval/datasets/northfield")


def ask(messages):
    call = ToolCall("t0", "raise_question", {"status": "unknown", "reason": "not enough evidence", "question": "Do you hold evidence for this requirement, such as a test report or certificate?"})
    return ToolTurn("", [call], [{"type": "tool_use", "id": "t0", "name": call.name, "input": call.input}],
                    Usage("claude-opus-5-5", 1500, 80), "tool_use")


@pytest.fixture
def eval_env(monkeypatch, _test_db, tmp_path):
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    monkeypatch.setattr("tenderfit.eval.run.EVAL_DB", "tenderfit_eval_test")
    monkeypatch.setattr("tenderfit.config.settings.document_dir", str(tmp_path / "docs"))
    return tmp_path


def test_harness_runs_baselines_and_amendments(eval_env):
    kw = dict(emb=BagEmbedder(), rr=OverlapReranker())
    results, summary = {}, {}
    for name in ("B0", "B1", "B5", "B5-full"):
        rec = RecordedLLM(eval_env / f"{name}.json", live=ScriptedLLM(verdicts, ask))
        results[name] = run_config(DATASET, BASELINES[name], rec, date(2026, 10, 14), **kw)
        summary[name] = summarise(results[name])
    b1 = summary["B1"]
    assert b1["extraction_recall_mandatory"] == 1.0
    assert (b1["blocker_recall"], b1["false_blocker_rate"], b1["quote_match_rate"]) == (1.0, 0.0, 1.0)
    assert b1["final_with_essential_unread"] == 0 and b1["stale_fact_unflagged"] == 0
    assert b1["false_met_rate"] == 0.0 and b1["extraction_precision"] == 1.0 and b1["model_calls"] > 0
    assert summary["B0"]["false_blocker_rate"] > b1["false_blocker_rate"], "B0 has no guard: missing evidence becomes NOT MET"
    assert summary["B0"]["quote_match_rate"] < 1.0
    b5, full = summary["B5"], summary["B5-full"]
    assert (b5["affected_recall"], b5["affected_precision"], b5["missed_change_rate"]) == (1.0, 1.0, 0.0)
    assert full["affected_recall"] == 1.0 and full["affected_precision"] < 1.0
    assert b1["citation_entailment"] is None and b1["retrieval_recall_at_5"] is None, "unmeasured is reported as unmeasured"
    lines = decisions(results, summary)
    assert any(l.startswith("Keep targeted reassessment (B5 over B5-full): yes") for l in lines)
    md = report(DATASET, summary, lines, "smoke test")
    assert "| blocker_recall |" in md and "Pre-registered decision rules" in md

    replay = RecordedLLM(eval_env / "B1.json")
    again = summarise(run_config(DATASET, BASELINES["B1"], replay, date(2026, 10, 14), **kw))
    assert again["macro_f1"] == b1["macro_f1"], "replaying recordings reproduces the run without a model"
