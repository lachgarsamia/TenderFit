"""One agent that answers a supplier's what-if question by building, running and explaining one simulation.

It is the only part of Mode B that calls a model, and the model only ever proposes: every hypothetical change goes through
the same validation as a person's (store.add_change), every verdict comes from the rule engine (engine.run), and the
final answer's claims about what changed are checked against the stored results before they are accepted. The model
has no tool that reaches a live table, and nothing it does is written anywhere but this simulation's own rows: its
trace and costs go to simulation_agent_step, not agent_step or llm_call.

A single agent with a fixed tool list, not a planner and workers: the task is one short, linear investigation.

A session is bounded three ways before every model call: its deadline (no call starts with under 15 s left, and each
call's timeout ends before the deadline), its own spend and the supplier's rolling 24-hour spend (spend.admit_call,
counting the call's worst case). A call is never retried by the SDK, so the deadline holds; a failure ends the session
as agent_unavailable, recorded as an llm_error step whose cost is the call's worst case when it may have been billed.
In the background (Phase 2) begin() runs in the API request and investigate() in the worker; ask() is both at once."""
import inspect
import json
import time
from collections.abc import Callable
from datetime import date

from psycopg.types.json import Jsonb

from tenderfit.agent.loop import Budget
from tenderfit.config import settings
from tenderfit.db import Conn
from tenderfit.llm import LLM, UNTRUSTED_RULE, LLMError, Usage, untrusted
from tenderfit.simulation import engine, spend, store
from tenderfit.simulation.changes import KINDS, SimulationError
from tenderfit.simulation.isolation import read_only, require_idle
from tenderfit.simulation.snapshot import Snapshot

AGENT_VERSION = "sim-agent-v1"
MAX_TOOL_CALLS = 12
MAX_QUESTION = 2000


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": props, "required": required or list(props)}


CHANGE_HELP = """change is one hypothetical change as a JSON object string, with "kind" and its fields:
set_condition_bound {requirement_id, condition (ordinal), bound (number)}: a numeric threshold in a requirement;
set_condition_match {requirement_id, condition, match}: which standard or certificate a requirement names;
set_fact_validity {fact_id, valid_until (YYYY-MM-DD or null)}; set_fact_evidence {fact_id, evidence_status
(document_on_file, declared, missing, unknown, needs_update)}; remove_fact {fact_id};
set_fact_value {fact_id, key, value}: one value inside a profile fact, e.g. key lead_days;
add_fact {attribute, label, value (object), evidence_status, valid_until}: a fact the supplier does not hold yet,
referred to afterwards by the negative id add_change returns;
set_notice_field {field (notice.deadline or lot.quantity), value};
set_requirement_text {requirement_id, statement, quote}: new wording, which only the model could re-assess."""

TOOLS = [
    {"name": "read_baseline", "description": "The assessment being simulated: its requirements with ids, baseline verdicts and "
                                             "machine-checkable conditions (ordinal, kind, fact, bound, match), the tender's "
                                             "deadline and quantity, and the recommendation.",
     "input_schema": _obj({})},
    {"name": "read_facts", "description": "The supplier's profile facts as frozen for this simulation, with ids. attribute "
                                          "filters (capacity, experience, certificate, test_report, insurance, region, product, "
                                          "service, registration) or null for all.",
     "input_schema": _obj({"attribute": {"anyOf": [{"type": "string"}, {"type": "null"}]}})},
    {"name": "add_change", "description": "Record one hypothetical change. It is checked against the snapshot and the changes "
                                          "already recorded; an error says what to fix. " + CHANGE_HELP,
     # A string, not an object: strict tool schemas need every property of an object declared, and the nine kinds have
     # different fields (add_fact's value is free-form). store.add_change validates it strictly either way.
     "input_schema": _obj({"change": {"type": "string"}})},
    {"name": "preview", "description": "Which requirements the recorded changes can reach, and why. Run it before run_simulation.",
     "input_schema": _obj({})},
    {"name": "run_simulation", "description": "Run the rule engine on the snapshot with and without the changes. A simulation runs "
                                              "once; no change can be added afterwards.",
     "input_schema": _obj({})},
    {"name": "finish", "description": "Answer the question from the simulation's results. changed must list exactly the "
                                      "requirements whose verdict changed, with from and to statuses as run_simulation reported.",
     "input_schema": _obj({"answer": {"type": "string"},
                           "changed": {"type": "array", "items": _obj({"requirement_id": {"type": "integer"},
                                                                       "from": {"type": "string"}, "to": {"type": "string"}})},
                           "caveats": {"type": "array", "items": {"type": "string"}}})},
    {"name": "cannot_simulate", "description": "Finish without running when the question cannot be expressed as these changes "
                                               "(for example price, or anything the requirements do not test).",
     "input_schema": _obj({"reason": {"type": "string"}})},
]

SYSTEM = f"""You answer a UK public-tender supplier's "what if" question about one of their completed assessments by running
a simulation. A simulation is a hypothetical copy: it never changes the real assessment, the supplier's profile or the
tender, and its result is never a real verdict.

{UNTRUSTED_RULE}

Read the baseline and the facts, express the question as the smallest set of hypothetical changes ({', '.join(KINDS)}),
add them one at a time, preview, run, then finish. Change only what the question asks about. Every verdict comes from
run_simulation; never state a result it did not report. "needs_model" means the rules cannot decide that requirement
without a model reading the text: say so, do not guess. If drift or stale baselines are reported, mention them. If the
question cannot be expressed as these changes, use cannot_simulate. You have at most {MAX_TOOL_CALLS} tool calls."""


def check_question(question: str) -> str:
    question = question.strip()
    if not question or len(question) > MAX_QUESTION:
        raise SimulationError(f"ask a question of 1 to {MAX_QUESTION} characters")
    return question


def begin(conn: Conn, profile_id: int, baseline_assessment_id: int, question: str, created_by: str,
          as_of: date | None = None) -> int:
    """Create the simulation and record the question as its first step. No model call. Returns the simulation id."""
    question = check_question(question)
    sid = store.create(conn, profile_id, baseline_assessment_id, created_by, question[:500], as_of)
    try:
        _Session(conn, profile_id, sid, Budget()).record("question", {"question": question}, {})
    except BaseException:
        store.discard(conn, profile_id, sid)  # never a simulation without its question, waiting for an agent
        raise
    return sid


def ask(conn: Conn, profile_id: int, baseline_assessment_id: int, question: str, llm: LLM, created_by: str,
        as_of: date | None = None, budget: Budget | None = None) -> dict:
    """Create a simulation for the question and let the agent build, run and explain it. Returns the outcome."""
    sid = begin(conn, profile_id, baseline_assessment_id, question, created_by, as_of)
    return investigate(conn, profile_id, sid, llm, budget)


def investigate(conn: Conn, profile_id: int, simulation_id: int, llm: LLM, budget: Budget | None = None, *,
                clock: Callable[[], float] = time.monotonic) -> dict:
    """Let the agent build, run and explain the simulation begin() created. A simulation that is no longer waiting for
    its agent (discarded, run by hand, or already investigated) is left alone: nothing is written and no model called."""
    sim = store.get(conn, profile_id, simulation_id)
    steps = conn.execute("SELECT tool, input FROM simulation_agent_step WHERE simulation_id = %s ORDER BY step",
                         (simulation_id,)).fetchall()
    conn.rollback()
    if sim["status"] != "created" or [s["tool"] for s in steps] != ["question"]:
        reason = f"the simulation is {sim['status']}" if sim["status"] != "created" else (
            "it has no question" if not steps else "it was already investigated")
        return {"outcome": "skipped", "simulation_id": simulation_id, "agent_version": AGENT_VERSION, "reason": reason}
    question = steps[0]["input"]["question"]
    session = _Session(conn, profile_id, simulation_id, budget or Budget(
        cost_cap=settings.simulation_agent_session_usd, time_cap=settings.simulation_agent_deadline_s))
    session.step = len(steps)
    bounded = _takes_call_options(llm)
    deadline, started = settings.simulation_agent_deadline_s, clock()
    model, max_tokens = settings.llm_model_agent, settings.simulation_agent_max_tokens
    messages: list[dict] = [{"role": "user", "content": f"Question: {question}"}]
    for _ in range(MAX_TOOL_CALLS + 2):
        if (why := session.budget.exhausted()) or session.calls >= MAX_TOOL_CALLS:
            return session.end("budget", f"stopped: {why or 'tool-call limit'} reached before an answer")
        remaining = deadline - (clock() - started)
        if remaining < spend.MIN_CALL_S:
            return session.end("budget", "stopped: the session's time limit was reached before an answer")
        est_input = spend.input_tokens_estimate(SYSTEM, messages, TOOLS)
        if refused := spend.admit_call(conn, profile_id, simulation_id, spend.worst_case(model, est_input, max_tokens)):
            return session.end("budget", f"stopped: {refused} before an answer")
        options = {"timeout": min(remaining - spend.CALL_MARGIN_S, settings.llm_timeout_s), "max_retries": 0,
                   "max_tokens": max_tokens} if bounded else {}
        try:
            turn = llm.tool_turn("agent", SYSTEM, messages, TOOLS, **options)
        except LLMError as e:
            # A response that came back unusable carries its real usage. A call that failed in flight may still have been
            # processed and billed: count its worst case (estimated input, max_tokens output), so the account errs high.
            known: Usage | None = getattr(e, "usage", None)
            billed = known is None and bool(getattr(e, "may_have_billed", False))
            spent = known or (Usage(model, input_tokens=est_input, output_tokens=max_tokens) if billed else Usage(model))
            cost = known.cost_usd if known else spend.worst_case(model, est_input, max_tokens) if billed else 0.0
            session.budget.spend(cost, spent.input_tokens + spent.output_tokens)
            session.record("llm_error", {"estimated_input_tokens": est_input}, {"error": str(e)[:500], "cost_estimated": billed},
                           spent, cost=cost)
            return session.end("agent_unavailable", f"the model could not be reached ({e}); nothing was guessed")
        session.budget.spend(turn.usage.cost_usd, turn.usage.input_tokens + turn.usage.output_tokens)
        messages.append({"role": "assistant", "content": turn.raw_content})
        if not turn.calls:
            session.record("no_tool", {"text": turn.text[:2000]}, {}, turn.usage)
            messages.append({"role": "user", "content": "Use a tool. Finish with finish or cannot_simulate."})
            continue
        results = []
        for i, call in enumerate(turn.calls):
            usage = turn.usage if i == 0 else None
            session.calls += 1
            out, done = session.call(call.name, call.input)
            session.record(call.name, call.input, out, usage)
            if done:
                return session.outcome
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(out, default=str),
                            **({"is_error": True} if "error" in out else {})})
        messages.append({"role": "user", "content": results})
    return session.end("budget", "stopped: no answer within the step limit")


def _takes_call_options(llm: LLM) -> bool:
    """Whether this model client accepts per-call bounds (the real one, the recorder and the demo do). A client that
    does not is called exactly as before: test fakes written against the plain protocol keep working."""
    try:
        params = inspect.signature(llm.tool_turn).parameters
    except (TypeError, ValueError):
        return False
    return all(k in params for k in ("timeout", "max_retries", "max_tokens")) or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def answer(conn: Conn, profile_id: int, simulation_id: int) -> dict | None:
    """The accepted final answer of a simulation's agent, if it gave one."""
    store.get(conn, profile_id, simulation_id)
    row = conn.execute("""SELECT output FROM simulation_agent_step WHERE simulation_id = %s AND tool IN ('finish', 'cannot_simulate', 'end')
                          AND output ? 'outcome' ORDER BY step DESC LIMIT 1""", (simulation_id,)).fetchone()
    conn.rollback()
    return row["output"] if row else None


def trace(conn: Conn, profile_id: int, simulation_id: int) -> list[dict]:
    store.get(conn, profile_id, simulation_id)
    rows = conn.execute("SELECT * FROM simulation_agent_step WHERE simulation_id = %s ORDER BY step", (simulation_id,)).fetchall()
    conn.rollback()
    return rows


class _Session:
    def __init__(self, conn: Conn, profile_id: int, simulation_id: int, budget: Budget):
        self.conn, self.profile_id, self.sid, self.budget = conn, profile_id, simulation_id, budget
        self.calls, self.step = 0, 0
        self.summary: dict | None = None
        self.outcome: dict = {}

    # --- trace ---------------------------------------------------------------------------------------------------------

    def record(self, tool: str, inp: dict, out: dict, usage: Usage | None = None, cost: float | None = None) -> None:
        """Each step is its own small transaction, so the trace survives a failure later in the session. cost overrides
        the usage's own (an estimate for a failed call, priced conservatively by spend)."""
        require_idle(self.conn)
        self.step += 1
        try:
            self.conn.execute(
                """INSERT INTO simulation_agent_step (simulation_id, step, tool, input, output, model, input_tokens, output_tokens,
                                                      cost_usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (self.sid, self.step, tool, Jsonb(_jsonable(inp)), Jsonb(_jsonable(out)), usage.model if usage else None,
                 usage.input_tokens if usage else 0, usage.output_tokens if usage else 0,
                 cost if cost is not None else usage.cost_usd if usage else 0))
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def end(self, outcome: str, reason: str) -> dict:
        self.outcome = self._result(outcome, reason=reason)
        self.record("end", {}, self.outcome)
        return self.outcome

    def _result(self, outcome: str, **extra) -> dict:
        return {"outcome": outcome, "simulation_id": self.sid, "agent_version": AGENT_VERSION, "cost_usd": round(self.budget.cost, 6),
                "tool_calls": self.calls, **extra}

    # --- tools ---------------------------------------------------------------------------------------------------------

    def call(self, name: str, inp: dict) -> tuple[dict, bool]:
        """(tool output, whether the session is over). Every failure is reported to the model, never raised past here."""
        handlers: dict[str, Callable[..., dict]] = {"read_baseline": self.read_baseline, "read_facts": self.read_facts, "add_change": self.add_change,
                    "preview": self.preview, "run_simulation": self.run_simulation}
        try:
            if name == "finish":
                return self.finish(**inp)
            if name == "cannot_simulate":
                reason = str(inp.get("reason", "")).strip()[:2000] or "not stated"
                self.outcome = self._result("unsupported", reason=reason)
                return self.outcome, True
            if name not in handlers:
                return {"error": f"unknown tool {name}"}, False
            return handlers[name](**inp), False
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}, False
        except SimulationError as e:
            return {"error": str(e)}, False

    def _snapshot(self) -> tuple[dict, Snapshot]:
        sim = store.get(self.conn, self.profile_id, self.sid)
        return sim, Snapshot.from_json(sim["snapshot"])

    def read_baseline(self) -> dict:
        with read_only(self.conn):
            sim, snap = self._snapshot()
            rec = self.conn.execute("SELECT recommendation FROM assessment WHERE id = %s", (snap.assessment_id,)).fetchone()
        reqs = [{"requirement_id": r["id"], "obligation": r["obligation"], "category": r["category"],
                 "baseline_status": r["baseline_status"],
                 "text": untrusted("tender_passage", f"{r['statement']}\n{r['quote']}", id=r["passage_id"]),
                 "conditions": [{"ordinal": c["ordinal"], **({k: c["condition"].get(k) for k in ("kind", "fact", "bound", "match")}
                                                            if c["condition"] else {"kind": None, "note": "read by the model only"})}
                                for c in r["conditions"]]} for r in snap.requirements]
        return {"as_of": snap.as_of, "deadline": snap.fields.get("notice.deadline"), "quantity": snap.fields.get("lot.quantity"),
                "recommendation": rec["recommendation"] if rec else None, "requirements": reqs, "drift": sim["drift"]}

    def read_facts(self, attribute: str | None = None) -> dict:
        with read_only(self.conn):
            _, snap = self._snapshot()
        return {"facts": [{"id": f.id, "attribute": f.attribute, "label": f.label, "value": f.value,
                           "evidence_status": f.evidence_status, "valid_until": f.valid_until}
                          for f in snap.facts if not attribute or f.attribute == attribute]}

    def add_change(self, change: str) -> dict:
        try:
            change = json.loads(change)
        except (TypeError, json.JSONDecodeError) as e:
            return {"error": f"change must be a JSON object string: {e}"}
        ordinal = store.add_change(self.conn, self.profile_id, self.sid, change)
        added = [c for c in store.get(self.conn, self.profile_id, self.sid)["changes"] if c["kind"] == "add_fact"]
        self.conn.rollback()
        out: dict = {"recorded": True, "ordinal": ordinal}
        if isinstance(change, dict) and change.get("kind") == "add_fact":
            out["fact_id"] = -len(added)
        return out

    def preview(self) -> dict:
        reached = engine.preview(self.conn, self.profile_id, self.sid)
        return {"reached": [{"requirement_id": rid, "why": [x["detail"] for x in why]} for rid, why in reached.items() if why],
                "not_reached": [rid for rid, why in reached.items() if not why]}

    def run_simulation(self) -> dict:
        out = engine.run(self.conn, self.profile_id, self.sid)
        self.summary = out["summary"]
        rows = store.results(self.conn, self.profile_id, self.sid)
        self.conn.rollback()
        return {"summary": self.summary,
                "requirements": [_result_row(r) for r in rows]}

    def finish(self, answer: str, changed: list[dict], caveats: list[str]) -> tuple[dict, bool]:
        """Accepted only if its claims are the results: which verdicts changed, from what, to what."""
        if self.summary is None:
            return {"error": "run the simulation before answering, or use cannot_simulate"}, False
        actual = {(c["requirement_id"], c["from"], c["to"]) for c in self.summary["changed"]}
        claimed = {(c.get("requirement_id"), c.get("from"), c.get("to")) for c in changed}
        if claimed != actual:
            return {"error": "the changes you listed are not the simulation's results",
                    "actual_changes": self.summary["changed"]}, False
        rec = self.summary["recommendation"]
        own = []
        if ids := self.summary["needs_model"]:
            one = len(ids) == 1
            own.append(f"{_requirements(ids)} {'needs' if one else 'need'} a model to re-assess; "
                       f"the rules alone cannot say how the change affects {'it' if one else 'them'}")
        if self.summary["drift"]:
            own.append("the live profile changed since the baseline ran; those differences are drift, not part of this answer")
        if ids := self.summary["stale_baseline"]:
            own.append(f"the baseline verdict for {_requirements(ids)} may be out of date")
        self.outcome = self._result(
            "answered", answer=answer.strip()[:4000], changed=self.summary["changed"],
            recommendation={"before": rec["control"], "after": rec["simulated"], "complete": rec["complete"]},
            caveats=own + [str(c)[:500] for c in caveats][:10],
            notice="A hypothetical result. The real assessment is unchanged and this is not a verdict.")
        return self.outcome, True


def _requirements(ids: list[int]) -> str:
    return ("requirement " if len(ids) == 1 else "requirements ") + ", ".join(map(str, ids))


def _result_row(r: dict) -> dict:
    """What the model is shown for one requirement. A requirement no change reaches keeps its verdict, and says so,
    including one only the model could decide, whose baseline verdict then stands unchanged."""
    out = {"requirement_id": r["requirement_id"], "how": r["how"], "baseline": r["baseline_status"],
           "control": r["control_status"], "simulated": r["simulated_status"],
           "reason": (r["simulated"].get("simulated") or {}).get("reason"), "notes": r["simulated"].get("notes", [])}
    if r["how"] == "not_affected":
        out["meaning"] = "no change reaches it, so its verdict is unchanged" + (
            f": its baseline verdict ({r['baseline_status']}) stands" if r["control_status"] is None else "")
    elif r["how"] == "needs_model":
        out["meaning"] = "a change reaches it, but only a model reading the text could say its new verdict"
    return out


def _jsonable(v):
    return json.loads(json.dumps(v, default=str))
