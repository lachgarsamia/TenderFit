"""The what-if agent: a model proposes changes and explains results; validation, the rule engine and a claims check decide.

The model is scripted, so every test is deterministic and free. Each test that runs the agent also compares
live_fingerprint, which covers llm_call and agent_step: the agent's own trace and costs must stay in simulation tables."""
import json

import psycopg
import pytest

from tenderfit.agent.loop import Budget
from tenderfit.llm import LLMError, ToolCall, ToolTurn, Usage
from tenderfit.simulation import agent, store
from tenderfit.simulation.changes import SimulationError
from tests.test_isolation import world  # noqa: F401  (fixture)
from tests.test_pipeline import TODAY
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint

base = _base  # the fixture, shared with test_simulation.py
QUESTION = "What if the buyer accepted our 45-day lead time?"


class ScriptedAgent:
    """Plays one tool call (or a list of them) per turn. A step may be a function of the conversation so far."""

    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def json(self, *args, **kwargs):
        raise AssertionError("the agent only uses tool turns")

    def tool_turn(self, role, system, messages, tools):
        self.seen.append(list(messages))
        step = self.steps.pop(0) if self.steps else ("read_baseline", {})
        if isinstance(step, Exception):
            raise step
        step = step(messages) if callable(step) else step
        calls = [step] if isinstance(step, tuple) else step
        return ToolTurn("", [ToolCall(f"c{len(self.seen)}_{i}", n, inp) for i, (n, inp) in enumerate(calls)],
                        [{"type": "text", "text": "..."}], Usage("claude-opus-5-5", 1000, 200), "tool_use")

    def last_results(self) -> list[dict]:
        return self.seen[-1][-1]["content"]


def _j(change: dict) -> str:
    """The add_change tool takes the change as a JSON string, as the real tool schema requires."""
    return json.dumps(change)


def lead_45(b):
    return ("add_change", {"change": _j({"kind": "set_condition_bound", "requirement_id": b["lead"], "condition": 0, "bound": 45})})


def happy(b):
    return [("read_baseline", {}), lead_45(b), ("preview", {}), ("run_simulation", {}),
            ("finish", {"answer": "Lead time would no longer be a blocker, but it must be confirmed.",
                        "changed": [{"requirement_id": b["lead"], "from": "not_met", "to": "needs_confirmation"}],
                        "caveats": []})]


def ask(b, llm, **kw):
    return agent.ask(b["db"], b["atlas"], b["a"], QUESTION, llm, "tester", as_of=TODAY, **kw)


# --- The happy path, and what it must not touch -----------------------------------------------------------------------------

def test_the_agent_answers_from_the_simulation_and_writes_nothing_live(base):
    db = base["db"]
    before = live_fingerprint(db)
    out = ask(base, ScriptedAgent(happy(base)))
    assert live_fingerprint(db) == before, "no llm_call, agent_step, verdict or profile row: everything stays in simulation tables"

    assert out["outcome"] == "answered"
    assert out["changed"] == [{"requirement_id": base["lead"], "statement": "Deliver all desks within 30 days of order",
                               "obligation": "mandatory", "from": "not_met", "to": "needs_confirmation"}]
    assert out["recommendation"]["before"].startswith("Blocker found")
    assert not out["recommendation"]["after"].startswith("Blocker found") and out["recommendation"]["complete"]
    assert "not a verdict" in out["notice"]
    sim = store.get(db, base["atlas"], out["simulation_id"])
    db.rollback()
    assert sim["status"] == "completed" and sim["title"] == QUESTION and len(sim["changes"]) == 1
    assert agent.answer(db, base["atlas"], out["simulation_id"]) == out


def test_the_model_is_told_an_unreached_requirement_keeps_its_verdict(base):
    """Found with the real model: shown blank statuses for the installation requirement, it called it "needs model"."""
    llm = ScriptedAgent(happy(base))
    ask(base, llm)
    shown = json.loads(llm.seen[4][-1]["content"][0]["content"])
    install = next(r for r in shown["requirements"] if r["requirement_id"] == base["install"])
    assert install["how"] == "not_affected" and "baseline verdict (met) stands" in install["meaning"]


def test_generated_caveats_read_correctly(base):
    deadline = ("add_change", {"change": _j({"kind": "set_notice_field", "field": "notice.deadline", "value": "2099-06-30"})})
    finish = ("finish", {"answer": "No verdict changes.", "changed": [], "caveats": []})
    out = ask(base, ScriptedAgent([deadline, ("run_simulation", {}), finish]))
    assert out["caveats"][0] == (f"requirement {base['install']} needs a model to re-assess; "
                                 "the rules alone cannot say how the change affects it")


def test_the_trace_records_every_step_and_its_cost(base):
    out = ask(base, ScriptedAgent(happy(base)))
    steps = agent.trace(base["db"], base["atlas"], out["simulation_id"])
    assert [s["tool"] for s in steps] == ["question", "read_baseline", "add_change", "preview", "run_simulation", "finish"]
    assert steps[0]["input"] == {"question": QUESTION}
    assert sum(float(s["cost_usd"]) for s in steps) == pytest.approx(out["cost_usd"]) and out["cost_usd"] > 0
    assert all(s["model"] == "claude-opus-5-5" for s in steps[1:])


def test_tender_text_reaches_the_model_marked_as_untrusted(base):
    llm = ScriptedAgent(happy(base))
    ask(base, llm)
    baseline = llm.seen[1][-1]["content"][0]["content"]
    assert '<tender_passage id=' in baseline and "Deliver all desks within 30 days" in baseline


# --- The model proposes; the system decides ----------------------------------------------------------------------------------

def test_an_answer_whose_claims_are_not_the_results_is_refused(base):
    wrong = ("finish", {"answer": "Everything is met now.", "caveats": [],
                        "changed": [{"requirement_id": base["lead"], "from": "not_met", "to": "met"}]})
    llm = ScriptedAgent(happy(base)[:-1] + [wrong] + happy(base)[-1:])
    out = ask(base, llm)
    rejected = llm.seen[-1][-1]["content"][0]
    assert rejected.get("is_error") and "not the simulation's results" in rejected["content"]
    assert "needs_confirmation" in rejected["content"], "the model is shown what actually changed"
    assert out["outcome"] == "answered" and out["changed"][0]["to"] == "needs_confirmation"


def test_an_answer_before_any_run_is_refused(base):
    early = ("finish", {"answer": "It would pass.", "changed": [], "caveats": []})
    llm = ScriptedAgent([early] + happy(base))
    out = ask(base, llm)
    assert "run the simulation before answering" in llm.seen[1][-1]["content"][0]["content"]
    assert out["outcome"] == "answered"


def test_an_invalid_change_is_reported_back_and_can_be_corrected(base):
    bad = ("add_change", {"change": _j({"kind": "set_condition_bound", "requirement_id": base["lead"], "condition": 0, "bound": -5})})
    llm = ScriptedAgent([bad] + happy(base))
    out = ask(base, llm)
    err = llm.seen[1][-1]["content"][0]
    assert err.get("is_error") and "invalid change" in err["content"]
    assert out["outcome"] == "answered" and len(store.get(base["db"], base["atlas"], out["simulation_id"])["changes"]) == 1
    base["db"].rollback()


def test_the_agent_cannot_reach_another_suppliers_facts(base):
    db = base["db"]
    before = live_fingerprint(db)
    other = ("add_change", {"change": _j({"kind": "set_fact_value", "fact_id": base["other_fact"], "key": "amount_gbp", "value": 1})})
    llm = ScriptedAgent([other, ("cannot_simulate", {"reason": "no such fact"})])
    ask(base, llm)
    assert "not in this supplier's profile snapshot" in llm.seen[1][-1]["content"][0]["content"]
    assert live_fingerprint(db) == before


def test_a_tool_outside_the_list_does_nothing(base):
    db = base["db"]
    before = live_fingerprint(db)
    llm = ScriptedAgent([("update_fact", {"fact_id": base["facts"]["capacity"], "value": {"lead_days": 1}}),
                         ("record_verdict", {"status": "met"}), ("cannot_simulate", {"reason": "done"})])
    out = ask(base, llm)
    assert "unknown tool update_fact" in llm.seen[1][-1]["content"][0]["content"]
    assert "unknown tool record_verdict" in llm.seen[2][-1]["content"][0]["content"]
    assert out["outcome"] == "unsupported" and live_fingerprint(db) == before


def test_a_change_that_is_not_json_is_reported_back(base):
    llm = ScriptedAgent([("add_change", {"change": "{kind: set_condition_bound"}), ("cannot_simulate", {"reason": "stop"})])
    ask(base, llm)
    err = llm.seen[1][-1]["content"][0]
    assert err.get("is_error") and "JSON object string" in err["content"]


def test_every_tool_schema_is_valid_for_strict_tool_use():
    """Strict tool use requires every object schema to declare additionalProperties: false and list all properties as
    required. The real API refused an open object here; the scripted model never sends schemas, so check them directly."""
    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, path
                assert set(node.get("required", [])) == set(node.get("properties", {})), path
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    for t in agent.TOOLS:
        walk(t["input_schema"], t["name"])


def test_an_added_fact_is_given_its_id_so_later_changes_can_use_it(base):
    add = ("add_change", {"change": _j({"kind": "add_fact", "attribute": "test_report", "label": "BS EN 1729-2 report (2026)",
                                     "value": {"product": "ND-120", "standard": "BS EN 1729-2"}, "evidence_status": "declared"})})
    then = lambda messages: ("add_change", {"change": _j({  # noqa: E731
        "kind": "set_fact_evidence", "evidence_status": "document_on_file",
        "fact_id": json.loads(messages[-1]["content"][0]["content"])["fact_id"]})})
    llm = ScriptedAgent([add, then, ("cannot_simulate", {"reason": "stop"})])
    out = ask(base, llm)
    assert '"fact_id": -1' in llm.seen[1][-1]["content"][0]["content"]
    assert not llm.seen[2][-1]["content"][0].get("is_error")
    assert len(store.get(base["db"], base["atlas"], out["simulation_id"])["changes"]) == 2
    base["db"].rollback()


# --- Ending without an answer -----------------------------------------------------------------------------------------------

def test_a_question_the_changes_cannot_express_ends_unsupported(base):
    out = ask(base, ScriptedAgent([("read_baseline", {}), ("cannot_simulate", {"reason": "price is not assessed"})]))
    assert (out["outcome"], out["reason"]) == ("unsupported", "price is not assessed")
    assert store.get(base["db"], base["atlas"], out["simulation_id"])["status"] == "created"
    base["db"].rollback()
    assert store.results(base["db"], base["atlas"], out["simulation_id"]) == []
    base["db"].rollback()


def test_the_tool_call_limit_stops_the_agent(base):
    out = ask(base, ScriptedAgent([]))  # reads the baseline forever
    assert out["outcome"] == "budget" and out["tool_calls"] == agent.MAX_TOOL_CALLS
    assert agent.trace(base["db"], base["atlas"], out["simulation_id"])[-1]["tool"] == "end"


def test_the_cost_cap_stops_the_agent(base):
    out = ask(base, ScriptedAgent([]), budget=Budget(cost_cap=0.005))
    assert out["outcome"] == "budget" and "cost cap" in out["reason"]


def test_an_unreachable_model_is_reported_not_guessed(base):
    db = base["db"]
    before = live_fingerprint(db)
    out = ask(base, ScriptedAgent([LLMError("provider error 529")]))
    assert out["outcome"] == "agent_unavailable" and "nothing was guessed" in out["reason"]
    assert live_fingerprint(db) == before


def test_a_text_only_turn_is_nudged_and_recorded(base):
    class Chatty(ScriptedAgent):
        def tool_turn(self, role, system, messages, tools):
            if not self.seen:
                self.seen.append(list(messages))
                return ToolTurn("I think it would pass.", [], [{"type": "text", "text": "I think it would pass."}],
                                Usage("claude-opus-5-5", 10, 10), "end_turn")
            return super().tool_turn(role, system, messages, tools)

    out = ask(base, Chatty([("cannot_simulate", {"reason": "stop"})]))
    steps = agent.trace(base["db"], base["atlas"], out["simulation_id"])
    assert [s["tool"] for s in steps][:2] == ["question", "no_tool"]


def test_questions_are_bounded(base):
    with pytest.raises(SimulationError):
        agent.ask(base["db"], base["atlas"], base["a"], "   ", ScriptedAgent([]), "tester")
    with pytest.raises(SimulationError):
        agent.ask(base["db"], base["atlas"], base["a"], "x" * 2001, ScriptedAgent([]), "tester")


# --- The trace itself -------------------------------------------------------------------------------------------------------

def test_the_trace_is_append_only_and_closed_by_discarding(base):
    db = base["db"]
    out = ask(base, ScriptedAgent([("cannot_simulate", {"reason": "stop"})]))
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE simulation_agent_step SET tool = 'x' WHERE simulation_id = %s", (out["simulation_id"],))
    db.rollback()
    store.discard(db, base["atlas"], out["simulation_id"])
    with pytest.raises(psycopg.errors.RaiseException, match="discarded"):
        db.execute("INSERT INTO simulation_agent_step (simulation_id, step, tool) VALUES (%s, 99, 'late')", (out["simulation_id"],))
    db.rollback()


def test_another_supplier_cannot_read_the_answer_or_trace(base):
    out = ask(base, ScriptedAgent(happy(base)))
    with pytest.raises(SimulationError):
        agent.answer(base["db"], base["pennine"], out["simulation_id"])
    base["db"].rollback()
    with pytest.raises(SimulationError):
        agent.trace(base["db"], base["pennine"], out["simulation_id"])
    base["db"].rollback()
