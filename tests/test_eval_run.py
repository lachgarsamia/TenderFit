from datetime import date
from pathlib import Path

import pytest

from tenderfit.assessment.config import BASELINES
from tenderfit.eval.run import decisions, report, run_config, summarise
from tenderfit.llm import RecordedLLM
from tenderfit.llm import ToolCall, ToolTurn, Usage
from tests.fakes import ScriptedLLM
from tests.test_pipeline import verdicts
from tests.test_retrieval import BagEmbedder, OverlapReranker

DATASET = Path("eval/datasets/northfield")


def ask(messages):
    call = ToolCall("t0", "raise_question", {"status": "unknown", "reason": "not enough evidence", "question": "Can you confirm?"})
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
    for name in ("B1", "B2", "B4", "B4-full"):
        rec = RecordedLLM(eval_env / f"{name}.json", live=ScriptedLLM(verdicts, ask))
        results[name] = run_config(DATASET, BASELINES[name], rec, date(2026, 10, 14), **kw)
        summary[name] = summarise(results[name])
    b2 = summary["B2"]
    assert b2["extraction_recall_mandatory"] == 1.0
    assert (b2["blocker_recall"], b2["false_blocker_rate"], b2["quote_match_rate"]) == (1.0, 0.0, 1.0)
    assert b2["final_with_essential_unread"] == 0 and b2["stale_fact_unflagged"] == 0
    assert summary["B1"]["false_blocker_rate"] > b2["false_blocker_rate"], "B1 has no guard: missing evidence becomes NOT MET"
    assert summary["B1"]["quote_match_rate"] < 1.0
    b4, full = summary["B4"], summary["B4-full"]
    assert (b4["affected_recall"], b4["affected_precision"]) == (1.0, 1.0)
    assert full["affected_recall"] == 1.0 and full["affected_precision"] < 1.0
    lines = decisions(results, summary)
    assert any(l.startswith("Keep targeted reassessment (B4 over B4-full): yes") for l in lines)
    md = report(DATASET, summary, lines, "smoke test")
    assert "| blocker_recall |" in md and "Pre-registered decision rules" in md

    replay = RecordedLLM(eval_env / "B2.json")
    again = summarise(run_config(DATASET, BASELINES["B2"], replay, date(2026, 10, 14), **kw))
    assert again["macro_f1"] == b2["macro_f1"], "replaying recordings reproduces the run without a model"
