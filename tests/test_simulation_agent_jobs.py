"""Mode B, phase 2: what-if questions answered in the background by the existing queue and worker.

The request creates the simulation and queues one simulation_agent job (one attempt, no retry); the worker runs the
agent. Every model call is bounded by the session's deadline, its own spend cap and the supplier's rolling 24-hour
allowance, checked before the call. Agent jobs wait behind every other ready job. Nothing live is written but the job
row itself."""
import threading
from datetime import timedelta

import anthropic
import httpx
import pytest

from tenderfit.config import settings
from tenderfit.jobs import queue, worker
from tenderfit.llm import AnthropicLLM, LLMError, OutputTooLong, Usage, _billed, _provider_error
from tenderfit.simulation import agent, spend, store
from tests.conftest import as_supplier
from tests.test_isolation import world  # noqa: F401  (fixture)
from tests.test_pipeline import TODAY
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint
from tests.test_simulation_agent import QUESTION, ScriptedAgent, happy
from tests.test_simulation_api import api as _api
from tests.test_simulation_api import lead, new

base, api = _base, _api  # fixtures, shared with test_simulation.py and test_simulation_api.py


class Bounded(ScriptedAgent):
    """A model client that accepts the per-call bounds, as the real one does, and remembers them."""

    def __init__(self, steps, clock=None, step_s=0.0):
        super().__init__(steps)
        self.options, self.clock, self.step_s = [], clock, step_s

    def tool_turn(self, role, system, messages, tools, *, timeout=None, max_retries=None, max_tokens=None):
        self.options.append({"timeout": timeout, "max_retries": max_retries, "max_tokens": max_tokens})
        if self.clock:
            self.clock.t += self.step_s
        return super().tool_turn(role, system, messages, tools)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def outside_jobs(db) -> dict:
    """The live fingerprint without the job table: queuing a question writes exactly one job row, nothing else."""
    return {k: v for k, v in live_fingerprint(db).items() if k not in ("job", "seq:job_id_seq")}


def spent(db, profile_id: int, assessment: int, usd: float, hours_ago: float = 0) -> int:
    """Spend recorded against the supplier by an earlier question."""
    sid = store.create(db, profile_id, assessment, "earlier", as_of=TODAY)
    db.execute("""INSERT INTO simulation_agent_step (simulation_id, step, tool, model, cost_usd, created_at)
                  VALUES (%s, 1, 'read_baseline', 'claude-opus-5-5', %s, now() - %s)""", (sid, usd, timedelta(hours=hours_ago)))
    db.commit()
    return sid


def begun(b) -> int:
    return agent.begin(b["db"], b["atlas"], b["a"], QUESTION, "tester", as_of=TODAY)


def what_if(api, client="A", assessment=None, question=QUESTION):
    return api[client].post(f"/assessments/{assessment or api['a' if client == 'A' else 'p']}/what-if",
                            json={"question": question, "as_of": TODAY.isoformat()})


@pytest.fixture
def quiet_worker(monkeypatch):
    """run_once without its periodic scheduling, so the only jobs are the ones a test queues."""
    for name in ("schedule_import", "schedule_daily_check", "schedule_changes"):
        monkeypatch.setattr(worker, name, lambda conn, now: None)


def use_model(monkeypatch, llm):
    import tenderfit.llm
    monkeypatch.setattr(tenderfit.llm, "default_llm", lambda: llm)


# --- Settings and the shared infrastructure, unchanged for everything else ------------------------------------------------

def test_the_limits_are_the_planned_defaults():
    assert (settings.simulation_agent_daily_usd, settings.simulation_agent_session_usd, settings.simulation_agent_deadline_s,
            settings.simulation_agent_max_tokens, settings.simulation_agent_queue_max) == (5.00, 0.50, 150, 4096, 20)


class FakeClient:
    """Stands in for anthropic.Anthropic: records with_options and create calls."""

    def __init__(self, raise_=None, stop_reason="tool_use"):
        self.with_options_calls, self.creates, self.raise_, self.stop_reason = [], [], raise_, stop_reason
        self.beta = self
        self.messages = self

    def with_options(self, **kw):
        self.with_options_calls.append(kw)
        return self

    def create(self, **kw):
        self.creates.append(kw)
        if self.raise_:
            raise self.raise_
        usage = type("U", (), {"input_tokens": 1000, "output_tokens": 50, "cache_read_input_tokens": 0})()
        return type("R", (), {"model": "claude-opus-5-5", "usage": usage, "stop_reason": self.stop_reason, "content": []})()


def test_call_bounds_reach_one_call_and_mode_a_calls_are_unchanged():
    client = FakeClient()
    llm = AnthropicLLM(client=client)
    llm.tool_turn("agent", "s", [{"role": "user", "content": "q"}], [])
    assert client.with_options_calls == [] and client.creates[-1]["max_tokens"] == 16000, "no bounds: exactly as before"
    llm.tool_turn("agent", "s", [{"role": "user", "content": "q"}], [], timeout=42.0, max_retries=0, max_tokens=4096)
    assert client.with_options_calls == [{"timeout": 42.0, "max_retries": 0}] and client.creates[-1]["max_tokens"] == 4096
    with pytest.raises(LLMError, match="no text block"):  # the fake returns no content; only the request matters here
        llm.json("extract", "s", "u", {"type": "object"})
    assert len(client.with_options_calls) == 1 and client.creates[-1]["max_tokens"] == 16000, "Mode A's json calls too"


def test_provider_errors_say_whether_the_call_may_have_been_billed():
    req = httpx.Request("POST", "https://api.anthropic.invalid/v1/messages")

    def status(code):
        return anthropic.APIStatusError("x", response=httpx.Response(code, request=req), body=None)
    cases = [(anthropic.APITimeoutError(request=req), True), (anthropic.APIConnectionError(request=req), True),
             (status(529), True), (status(500), True), (status(408), True), (status(400), False), (status(429), False)]
    for error, billed in cases:
        with pytest.raises(LLMError) as e:
            AnthropicLLM(client=FakeClient(raise_=error)).tool_turn("agent", "s", [], [], max_retries=0)
        assert e.value.may_have_billed is billed, error
    with pytest.raises(OutputTooLong) as e:
        AnthropicLLM(client=FakeClient(stop_reason="max_tokens")).tool_turn("agent", "s", [], [])
    assert e.value.may_have_billed and e.value.usage.output_tokens == 50, "a response that came back carries its usage"


def test_agent_jobs_are_claimed_after_every_ready_job_of_another_kind(base):
    db = base["db"]
    first = queue.enqueue(db, "simulation_agent", {"profile_id": base["atlas"], "simulation_id": 0}, max_attempts=1)
    second = queue.enqueue(db, "simulation_agent", {"profile_id": base["pennine"], "simulation_id": 0}, max_attempts=1)
    other = queue.enqueue(db, "daily_check")
    db.commit()
    assert queue.waiting_behind(db, first) == 1 and queue.waiting_behind(db, second) == 2
    assert [queue.claim(db)["id"] for _ in range(3)] == [other, first, second]
    assert queue.waiting_behind(db, first) == 0, "running: nothing is ahead of it"
    db.rollback()


def test_one_attempt_means_no_automatic_retry_and_the_default_is_unchanged(base):
    db = base["db"]
    once = queue.enqueue(db, "simulation_agent", {}, max_attempts=1)
    default = queue.enqueue(db, "daily_check")
    db.commit()
    rows = {r["id"]: r for r in db.execute("SELECT id, max_attempts FROM job WHERE id = ANY(%s)", ([once, default],))}
    assert rows[once]["max_attempts"] == 1 and rows[default]["max_attempts"] == 5
    job = queue.claim(db)
    assert job["id"] == default
    queue.complete(db, default)
    job = queue.claim(db)
    queue.fail(db, job, "boom")
    assert db.execute("SELECT status FROM job WHERE id = %s", (once,)).fetchone()["status"] == "failed"
    db.rollback()


def test_an_agent_job_whose_worker_died_is_failed_after_minutes_not_rerun(base):
    db = base["db"]
    agent_job = queue.enqueue(db, "simulation_agent", {}, max_attempts=1)
    other = queue.enqueue(db, "daily_check")
    db.execute("UPDATE job SET status = 'running', attempts = 1, locked_at = now() - interval '15 minutes' WHERE id = ANY(%s)",
               ([agent_job, other],))
    db.commit()
    queue.claim(db)
    got = {r["id"]: r["status"] for r in db.execute("SELECT id, status FROM job WHERE id = ANY(%s)", ([agent_job, other],))}
    assert got == {agent_job: "failed", other: "running"}, "other kinds keep their one-hour window"
    db.rollback()


# --- The session's bounds ---------------------------------------------------------------------------------------------------

def test_each_call_is_bounded_by_the_time_left_with_no_sdk_retries(base):
    clock = Clock()
    llm = Bounded(happy(base), clock, step_s=10)
    out = agent.investigate(base["db"], base["atlas"], begun(base), llm, clock=clock)
    assert out["outcome"] == "answered"
    assert [o["timeout"] for o in llm.options] == [145, 135, 125, 115, 105]
    assert all(o["max_retries"] == 0 and o["max_tokens"] == 4096 for o in llm.options)


def test_the_per_call_timeout_never_exceeds_the_providers(base, monkeypatch):
    monkeypatch.setattr(settings, "llm_timeout_s", 60)
    llm = Bounded(happy(base))
    agent.investigate(base["db"], base["atlas"], begun(base), llm, clock=Clock())
    assert {o["timeout"] for o in llm.options} == {60}


def test_no_call_starts_near_the_deadline(base):
    clock = Clock()
    llm = Bounded(happy(base), clock, step_s=45)
    sid = begun(base)
    out = agent.investigate(base["db"], base["atlas"], sid, llm, clock=clock)
    assert out["outcome"] == "budget" and "time limit" in out["reason"]
    assert [o["timeout"] for o in llm.options] == [145, 100, 55, 10], "the fourth call ends 5 s before the deadline"
    assert agent.trace(base["db"], base["atlas"], sid)[-1]["tool"] == "end"


def test_the_daily_allowance_stops_a_session_before_the_call(base):
    db = base["db"]
    spent(db, base["atlas"], base["a"], 4.95)
    llm = Bounded(happy(base))
    sid = begun(base)
    out = agent.investigate(db, base["atlas"], sid, llm, clock=Clock())
    assert out["outcome"] == "budget" and "daily allowance" in out["reason"]
    assert llm.options == [], "no model call was made"
    assert [s["tool"] for s in agent.trace(db, base["atlas"], sid)] == ["question", "end"]


def test_spend_older_than_a_day_no_longer_counts(base):
    db = base["db"]
    spent(db, base["atlas"], base["a"], 4.95, hours_ago=25)
    assert spend.spent_24h(db, base["atlas"]) == 0
    db.rollback()
    assert agent.investigate(db, base["atlas"], begun(base), Bounded(happy(base)), clock=Clock())["outcome"] == "answered"


def test_spend_is_per_supplier(base):
    db = base["db"]
    spent(db, base["pennine"], base["p"], 4.95)
    assert spend.spent_24h(db, base["atlas"]) == 0
    db.rollback()
    assert agent.investigate(db, base["atlas"], begun(base), Bounded(happy(base)), clock=Clock())["outcome"] == "answered"


def test_the_session_cap_counts_each_calls_worst_case(base, monkeypatch):
    """The cap is set just above the first call's worst case. Each fake turn costs $0.008, and the conversation grows,
    so the second call's worst case no longer fits: it never starts, though actual spend is far below the cap."""
    first = spend.worst_case(settings.llm_model_agent, spend.input_tokens_estimate(
        agent.SYSTEM, [{"role": "user", "content": f"Question: {QUESTION}"}], agent.TOOLS), settings.simulation_agent_max_tokens)
    monkeypatch.setattr(settings, "simulation_agent_session_usd", first + 0.005)
    llm = Bounded(happy(base))
    sid = begun(base)
    out = agent.investigate(base["db"], base["atlas"], sid, llm, clock=Clock())
    assert out["outcome"] == "budget" and "this question's allowance" in out["reason"]
    assert len(llm.options) == 1 and spend.session_spent(base["db"], sid) == pytest.approx(0.008)
    base["db"].rollback()


def test_a_call_that_failed_in_flight_is_charged_its_worst_case(base):
    """Input estimated from the request's size, plus max_tokens of output: a call that timed out on our side may have
    been processed and billed in full (plan §4.2)."""
    db = base["db"]
    sid = begun(base)
    out = agent.investigate(db, base["atlas"], sid, Bounded([_provider_error("provider unreachable: timed out", True)]),
                            clock=Clock())
    assert out["outcome"] == "agent_unavailable" and "nothing was guessed" in out["reason"]
    err = [s for s in agent.trace(db, base["atlas"], sid) if s["tool"] == "llm_error"][0]
    assert err["output"]["cost_estimated"] is True and err["output"]["error"].startswith("provider unreachable")
    expected = spend.worst_case(settings.llm_model_agent, err["input"]["estimated_input_tokens"], 4096)
    assert float(err["cost_usd"]) == pytest.approx(expected, abs=1e-6) and expected > 0.08
    assert (err["input_tokens"], err["output_tokens"]) == (err["input"]["estimated_input_tokens"], 4096)
    assert spend.spent_24h(db, base["atlas"]) == pytest.approx(expected, abs=1e-6), "it counts against the day"
    db.rollback()


def test_a_refused_request_costs_nothing_and_a_truncated_one_its_real_usage(base):
    db = base["db"]
    sid = begun(base)
    agent.investigate(db, base["atlas"], sid, Bounded([_provider_error("provider error 400: bad", False)]), clock=Clock())
    err = [s for s in agent.trace(db, base["atlas"], sid) if s["tool"] == "llm_error"][0]
    assert float(err["cost_usd"]) == 0 and err["output"]["cost_estimated"] is False

    sid = begun(base)
    usage = Usage("claude-opus-5-5", 2000, 4096)
    agent.investigate(db, base["atlas"], sid, Bounded([_billed(OutputTooLong("hit max_tokens"), usage)]), clock=Clock())
    err = [s for s in agent.trace(db, base["atlas"], sid) if s["tool"] == "llm_error"][0]
    assert float(err["cost_usd"]) == pytest.approx(usage.cost_usd) and err["output"]["cost_estimated"] is False
    assert (err["input_tokens"], err["output_tokens"]) == (2000, 4096)


def test_a_simulation_no_longer_waiting_is_left_alone(base):
    db = base["db"]
    sid = begun(base)
    store.discard(db, base["atlas"], sid)
    llm = Bounded(happy(base))
    out = agent.investigate(db, base["atlas"], sid, llm, clock=Clock())
    assert out["outcome"] == "skipped" and "discarded" in out["reason"] and llm.options == []
    sid = begun(base)
    agent.investigate(db, base["atlas"], sid, Bounded(happy(base)), clock=Clock())
    again = agent.investigate(db, base["atlas"], sid, Bounded(happy(base)), clock=Clock())
    assert again["outcome"] == "skipped"


# --- Over HTTP, through the worker -------------------------------------------------------------------------------------------

def test_a_question_is_queued_answered_by_the_worker_and_writes_nothing_live(api, quiet_worker, monkeypatch):
    db, A = api["db"], api["A"]
    before = outside_jobs(db)
    r = what_if(api)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["hypothetical"] is True and set(body) >= {"simulation_id", "job_id"}
    job = A.get(f"/jobs/{body['job_id']}").json()
    assert job["kind"] == "simulation_agent" and job["status"] == "queued" and job["waiting_behind"] == 0
    assert db.execute("SELECT max_attempts, payload FROM job WHERE id = %s", (body["job_id"],)).fetchone() == {
        "max_attempts": 1, "payload": {"profile_id": api["atlas"], "simulation_id": body["simulation_id"]}}
    db.rollback()
    trace = A.get(f"/simulations/{body['simulation_id']}/trace").json()
    assert [s["tool"] for s in trace["steps"]] == ["question"] and trace["outcome"] is None

    llm = Bounded(happy(api))
    use_model(monkeypatch, llm)
    assert worker.run_once(db) is True
    job = A.get(f"/jobs/{body['job_id']}").json()
    assert job["status"] == "done" and job["attempts"] == 1 and job["waiting_behind"] == 0
    trace = A.get(f"/simulations/{body['simulation_id']}/trace").json()
    assert trace["outcome"]["outcome"] == "answered" and trace["job"] == {"id": body["job_id"], "status": "done"}
    assert [s["tool"] for s in trace["steps"]] == ["question", "read_baseline", "add_change", "preview", "run_simulation", "finish"]
    assert all(set(s) >= {"cost_usd", "cost_estimated", "tokens", "created_at"} for s in trace["steps"])
    assert all(o["max_retries"] == 0 for o in llm.options)
    assert A.get(f"/simulations/{body['simulation_id']}").json()["status"] == "completed"
    assert outside_jobs(db) == before, "only the job row is outside the simulation tables"


def test_jobs_of_other_kinds_have_no_waiting_count(api):
    db = api["db"]
    jid = queue.enqueue(db, "assess", {"profile_id": api["atlas"], "lot_id": api["lot"]})
    db.commit()
    assert "waiting_behind" not in api["A"].get(f"/jobs/{jid}").json(), "Mode A's response is unchanged"


def test_one_question_at_a_time_per_supplier(api):
    first = what_if(api).json()
    r = what_if(api)
    assert r.status_code == 409
    assert r.json()["detail"]["simulation_id"] == first["simulation_id"] and r.json()["detail"]["job_id"] == first["job_id"]
    assert what_if(api, "B").status_code == 202, "another supplier is not affected"


def test_two_simultaneous_questions_admit_exactly_one(api):
    clients = [as_supplier(api["db"], api["atlas"], f"twin{i}@test.example") for i in range(2)]
    gate, codes = threading.Barrier(2), []

    def go(c):
        gate.wait()
        codes.append(c.post(f"/assessments/{api['a']}/what-if", json={"question": QUESTION}).status_code)
    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(codes) == [202, 409]
    assert api["db"].execute("SELECT count(*) AS n FROM job WHERE kind = 'simulation_agent'").fetchone()["n"] == 1
    api["db"].rollback()


def test_the_daily_allowance_is_checked_at_admission(api):
    db = api["db"]
    spent(db, api["atlas"], api["a"], 4.60)
    sims_before = db.execute("SELECT count(*) AS n FROM simulation").fetchone()["n"]
    db.rollback()
    r = what_if(api)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 23 * 3600
    assert r.json()["detail"]["remaining_usd"] == pytest.approx(0.40)
    assert db.execute("SELECT count(*) AS n FROM simulation").fetchone()["n"] == sims_before, "nothing was created"
    db.rollback()
    allowance = api["A"].get("/what-if/allowance").json()
    assert allowance["can_ask"] is False and allowance["next_question_at"]


def test_a_full_queue_is_refused_with_retry_after(api, monkeypatch):
    monkeypatch.setattr(settings, "simulation_agent_queue_max", 1)
    assert what_if(api, "B").status_code == 202
    r = what_if(api)
    assert r.status_code == 429 and r.headers["Retry-After"] == "30"


def test_another_suppliers_assessment_and_jobs_are_not_found(api):
    assert what_if(api, "B", assessment=api["a"]).status_code == 404
    mine = what_if(api).json()
    assert api["B"].get(f"/jobs/{mine['job_id']}").status_code == 404
    assert api["B"].get(f"/simulations/{mine['simulation_id']}/trace").status_code == 404


def test_asking_needs_a_session_and_the_csrf_header(api):
    from fastapi.testclient import TestClient

    from tenderfit.api.main import app
    db = api["db"]
    before = live_fingerprint(db)
    anon = TestClient(app, base_url="http://testserver/v1", headers={"X-TenderFit": "1"})
    assert anon.post(f"/assessments/{api['a']}/what-if", json={"question": QUESTION}).status_code == 401
    assert anon.get("/what-if/allowance").status_code == 401
    r = api["A"].post(f"/assessments/{api['a']}/what-if", json={"question": QUESTION}, headers={"X-TenderFit": ""})
    assert r.status_code == 403 and "X-TenderFit" in r.json()["detail"]
    assert live_fingerprint(db) == before, "no simulation, no job"


def test_questions_are_validated(api):
    assert what_if(api, question="").status_code == 422
    assert what_if(api, question="   ").status_code == 422
    assert what_if(api, question="x" * 2001).status_code == 422
    assert api["A"].post(f"/assessments/{api['a']}/what-if", json={"question": "q", "profile_id": 1}).status_code == 422


def test_nobody_else_changes_or_runs_a_simulation_the_agent_is_working_on(api, quiet_worker, monkeypatch):
    db, A = api["db"], api["A"]
    body = what_if(api).json()
    sid = body["simulation_id"]
    assert A.post(f"/simulations/{sid}/changes", json={"change": lead(api)}).status_code == 409
    assert A.post(f"/simulations/{sid}/run").status_code == 409
    assert A.post(f"/simulations/{sid}/discard").status_code == 200, "it can always be abandoned"
    llm = Bounded(happy(api))
    use_model(monkeypatch, llm)
    worker.run_once(db)
    assert A.get(f"/jobs/{body['job_id']}").json()["status"] == "done" and llm.options == []
    assert [s["tool"] for s in A.get(f"/simulations/{sid}/trace").json()["steps"]] == ["question"]
    other = new(api)["id"]
    assert A.post(f"/simulations/{other}/changes", json={"change": lead(api)}).status_code == 201, "others are unaffected"


def test_a_failed_agent_job_is_not_retried(api, quiet_worker, monkeypatch):
    db = api["db"]
    body = what_if(api).json()

    class Broken:
        def tool_turn(self, *args, **kwargs):
            raise RuntimeError("bug in the client")
    use_model(monkeypatch, Broken())
    worker.run_once(db)
    job = api["A"].get(f"/jobs/{body['job_id']}").json()
    assert job["status"] == "failed" and job["attempts"] == 1 and "bug in the client" in job["last_error"]
    assert worker.run_once(db) is False, "nothing left to claim"
    assert what_if(api).status_code == 202, "the supplier can ask again"


def test_the_demo_model_ends_the_session_without_spending(api, quiet_worker, monkeypatch):
    from tenderfit.demo.llm import DemoLLM
    db = api["db"]
    body = what_if(api).json()
    use_model(monkeypatch, DemoLLM())
    worker.run_once(db)
    trace = api["A"].get(f"/simulations/{body['simulation_id']}/trace").json()
    assert trace["outcome"]["outcome"] == "agent_unavailable" and sum(s["cost_usd"] for s in trace["steps"]) == 0


def test_the_worker_closes_abandoned_simulation_runs(api, quiet_worker):
    db = api["db"]
    sid = new(api)["id"]
    db.execute("UPDATE simulation SET status = 'running', started_at = now() - interval '2 hours' WHERE id = %s", (sid,))
    db.commit()
    worker.run_once(db)
    assert store.get(db, api["atlas"], sid)["status"] == "failed"
    db.rollback()


def test_the_trace_of_a_hand_built_simulation_is_empty(api):
    sid = new(api)["id"]
    t = api["A"].get(f"/simulations/{sid}/trace").json()
    assert t["steps"] == [] and t["job"] is None and t["outcome"] is None and t["hypothetical"] is True

