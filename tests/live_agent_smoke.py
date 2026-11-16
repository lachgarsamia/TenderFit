"""Real-model smoke test for the what-if agent. It costs real money, so it is never collected automatically: the file
name does not match test_*.py. Run it by naming it:

    LIVE_LLM=1 bash scripts/test.sh tests/live_agent_smoke.py -s

It fails, rather than skips, without LIVE_LLM=1: CI treats any skipped test as a failed run.
The baseline is built with the scripted model as usual; only the agent talks to the real model."""
import json
import os

import pytest

from tenderfit.llm import AnthropicLLM
from tenderfit.simulation import agent, store
from tests.test_isolation import world  # noqa: F401  (fixture)
from tests.test_pipeline import TODAY
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint

base = _base

QUESTIONS = [
    "What if the buyer accepted our 45-day lead time?",
    "What if our BS EN 1729-2 test report were on file?",
    "What if the lot quantity were 400 desks?",
    "What if we cut our price by 10%?",
]


def test_live_agent(base):
    if os.environ.get("LIVE_LLM") != "1":
        pytest.fail("set LIVE_LLM=1 to call the real model; this test costs money")
    db = base["db"]
    llm = AnthropicLLM()
    before = live_fingerprint(db)
    report = []
    for q in QUESTIONS:
        out = agent.ask(db, base["atlas"], base["a"], q, llm, "smoke-test", as_of=TODAY)
        steps = agent.trace(db, base["atlas"], out["simulation_id"])
        rejected = [s["step"] for s in steps if isinstance(s["output"], dict) and "error" in s["output"]]
        sim = store.get(db, base["atlas"], out["simulation_id"])
        db.rollback()
        report.append({"question": q, "outcome": out["outcome"], "tools": [s["tool"] for s in steps],
                       "errors_returned": [(s["tool"], s["output"]["error"]) for s in steps
                                           if isinstance(s["output"], dict) and "error" in s["output"]],
                       "changes": sim["changes"], "changed": out.get("changed"), "recommendation": out.get("recommendation"),
                       "answer": out.get("answer") or out.get("reason"), "caveats": out.get("caveats"),
                       "cost_usd": out["cost_usd"], "rejected_steps": rejected})
    after = live_fingerprint(db)
    print(json.dumps(report, indent=1, default=str))
    print("LIVE FINGERPRINT UNCHANGED:", after == before)
    assert after == before
