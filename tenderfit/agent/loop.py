import json
import time
from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

from tenderfit.config import settings
from tenderfit.llm import LLM, LLMError, UNTRUSTED_RULE, log_call, untrusted
from tenderfit.profile.validity import check
from tenderfit.retrieval.search import Embedder, Reranker, search
from tenderfit.rules.engine import NEEDS, UNKNOWN, Condition, Context, Fact, evaluate

AGENT_VERSION = "agent-v1"


@dataclass
class Budget:
    cost_cap: float = field(default_factory=lambda: settings.lot_cost_cap_usd)
    token_cap: int = field(default_factory=lambda: settings.lot_token_cap)
    time_cap: float = field(default_factory=lambda: settings.lot_time_cap_s)
    cost: float = 0.0
    tokens: int = 0
    started: float = field(default_factory=time.monotonic)

    def spend(self, cost: float, tokens: int) -> None:
        self.cost += cost
        self.tokens += tokens

    @property
    def left(self) -> float:
        return self.cost_cap - self.cost

    def exhausted(self) -> str | None:
        if self.cost >= self.cost_cap:
            return "cost cap"
        if self.tokens >= self.token_cap:
            return "token cap"
        if time.monotonic() - self.started >= self.time_cap:
            return "time cap"
        return None


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": props, "required": required or list(props)}


NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
TOOLS = [
    {"name": "search_passages", "description": "Hybrid search over this notice version's documents. Returns passages with ids.",
     "input_schema": _obj({"query": {"type": "string"}})},
    {"name": "read_section", "description": "Return the full section (all passages under the same heading) around a passage id, "
                                            "or the section whose heading starts with a label such as 'Appendix B' or '5.1'.",
     "input_schema": _obj({"passage_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]}, "label": NULLABLE_STR})},
    {"name": "open_attachment", "description": "Look up a document listed or referenced by this notice (by label) and return "
                                               "its headings and first passages, or why it cannot be read.",
     "input_schema": _obj({"label": {"type": "string"}})},
    {"name": "check_clarifications", "description": "Search clarification logs and buyer answers for this notice.",
     "input_schema": _obj({"query": {"type": "string"}})},
    {"name": "read_profile", "description": "Return the supplier's profile facts with evidence status and validity. "
                                            "attribute filters (capacity, experience, certificate, test_report, insurance, "
                                            "region, product, service, registration) or null for all.",
     "input_schema": _obj({"attribute": NULLABLE_STR})},
    {"name": "compare", "description": "Run a deterministic rule-engine check against the profile.",
     "input_schema": _obj({"kind": {"type": "string", "enum": ["at_least", "at_most", "within_years", "region_covered",
                                                                "has_evidence", "min_amount"]},
                           "fact": {"type": "string"}, "bound": {"anyOf": [{"type": "number"}, {"type": "string"}, {"type": "null"}]},
                           "years": {"anyOf": [{"type": "integer"}, {"type": "null"}]}, "anchor": NULLABLE_STR,
                           "min_units": {"anyOf": [{"type": "number"}, {"type": "null"}]}, "match": NULLABLE_STR})},
    {"name": "record_verdict", "description": "Finish: record the verdict with citations (exact quotes from passages the tools returned).",
     "input_schema": _obj({"status": {"type": "string", "enum": ["met", "not_met", "needs_confirmation", "unknown"]},
                           "reason": {"type": "string"},
                           "supporting_fact_ids": {"type": "array", "items": {"type": "integer"}},
                           "contrary_fact_ids": {"type": "array", "items": {"type": "integer"}},
                           "citations": {"type": "array", "items": _obj({"passage_id": {"type": "integer"}, "quote": {"type": "string"}})},
                           "question": NULLABLE_STR})},
    {"name": "raise_question", "description": "Finish when evidence is insufficient: record a targeted question for the user.",
     "input_schema": _obj({"status": {"type": "string", "enum": ["needs_confirmation", "unknown"]},
                           "reason": {"type": "string"}, "question": {"type": "string"}})},
]

AGENT_SYSTEM = f"""You investigate one requirement of a UK public tender for a supplier-side screening tool, after a fixed
pipeline could not settle it. Decide where to look for evidence and when it is sufficient, using only the tools.

{UNTRUSTED_RULE}

Follow cross-references ("see Appendix C", "as set out in Section 5") with read_section or open_attachment, check
clarifications when the text accepts an equivalent or is ambiguous, read the profile, and use compare for any
machine-checkable condition. Never assume certificates, experience, prices or commitments that are not in the profile.
Missing evidence is never not_met. You may only cite passages a tool returned, with exact quotes. Finish with
record_verdict, or raise_question when the evidence is not there. You have at most {settings.agent_max_tool_calls} tool calls."""


def _passage_out(p: dict) -> dict:
    return {"passage_id": p["id"], "document": p.get("document"), "section": p.get("section"), "page": p.get("page"),
            "text": untrusted("tender_passage", p["text"][:1500], id=p["id"])}


class Tools:
    def __init__(self, conn: psycopg.Connection, version_id: int, facts: list[Fact], ctx: Context,
                 emb: Embedder | None, rr: Reranker | None):
        self.conn, self.version_id, self.facts, self.ctx, self.emb, self.rr = conn, version_id, facts, ctx, emb, rr
        self.seen: dict[int, dict] = {}
        self.read_docs: set[int] = set()

    def _rows(self, sql: str, args: tuple) -> list[dict]:
        rows = self.conn.execute(sql, args).fetchall()
        for r in rows:
            self.seen[r["id"]] = dict(r)
        return rows

    def search_passages(self, query: str) -> dict:
        hits = search(self.conn, self.version_id, query, k=5, emb=self.emb, rr=self.rr)
        for h in hits:
            self.seen[h.id] = {"id": h.id, "text": h.text, "section": h.section, "page": h.page, "document": h.document}
        return {"passages": [_passage_out(self.seen[h.id]) for h in hits]}

    def read_section(self, passage_id: int | None = None, label: str | None = None) -> dict:
        base = """SELECT p.id, p.text, p.section, p.page, p.ordinal, p.document_id, coalesce(d.filename, 'document') document
                  FROM passage p JOIN document d ON d.id = p.document_id WHERE d.version_id = %s"""
        if passage_id is not None:
            anchor = self.conn.execute(base + " AND p.id = %s", (self.version_id, passage_id)).fetchone()
            if not anchor:
                return {"error": "passage not in this notice version"}
            rows = self._rows(base + """ AND p.document_id = %s AND (p.section IS NOT DISTINCT FROM %s
                                         OR abs(p.ordinal - %s) <= 1) ORDER BY p.ordinal LIMIT 8""",
                              (self.version_id, anchor["document_id"], anchor["section"], anchor["ordinal"]))
        elif label:
            rows = self._rows(base + " AND lower(p.section) LIKE %s ORDER BY p.document_id, p.ordinal LIMIT 8",
                              (self.version_id, label.lower().strip() + "%"))
        else:
            return {"error": "give passage_id or label"}
        return {"passages": [_passage_out(r) for r in rows]} if rows else {"error": f"no section found for {label or passage_id}"}

    def open_attachment(self, label: str) -> dict:
        ref = self.conn.execute(
            """SELECT r.*, d.filename FROM document_ref r LEFT JOIN document d ON d.id = r.document_id
               WHERE r.version_id = %s AND (lower(r.label) LIKE %s OR lower(coalesce(d.filename, '')) LIKE %s)
               ORDER BY r.status = 'read' DESC LIMIT 1""", (self.version_id, f"%{label.lower()}%", f"%{label.lower()}%")).fetchone()
        if not ref:
            return {"error": f"no document called {label} is listed or referenced in this notice"}
        self.read_docs.add(ref["id"])
        if ref["status"] != "read":
            return {"document": ref["label"], "status": ref["status"], "note": ref["note"],
                    "essential": ref["essential"], "readable": False}
        rows = self._rows("""SELECT p.id, p.text, p.section, p.page, coalesce(d.filename, 'document') document FROM passage p
                             JOIN document d ON d.id = p.document_id WHERE p.document_id = %s ORDER BY p.ordinal LIMIT 6""",
                          (ref["document_id"],))
        headings = [r["section"] for r in self.conn.execute(
            "SELECT DISTINCT section, min(ordinal) o FROM passage WHERE document_id = %s GROUP BY section ORDER BY o",
            (ref["document_id"],)) if r["section"]]
        return {"document": ref["label"], "status": "read", "headings": headings[:30], "passages": [_passage_out(r) for r in rows]}

    def check_clarifications(self, query: str) -> dict:
        docs = [r["document_id"] for r in self.conn.execute(
            """SELECT document_id FROM document_ref WHERE version_id = %s AND status = 'read'
               AND (ocds_type = 'clarifications' OR lower(label) LIKE '%%clarif%%')""", (self.version_id,))]
        if not docs:
            return {"clarifications": [], "note": "no clarification log has been published or uploaded for this notice"}
        hits = [h for h in search(self.conn, self.version_id, query, k=10, emb=self.emb, rr=self.rr) if h.document_id in docs][:5]
        for h in hits:
            self.seen[h.id] = {"id": h.id, "text": h.text, "section": h.section, "page": h.page, "document": h.document}
        self.read_docs |= set(docs)
        return {"clarifications": [_passage_out(self.seen[h.id]) for h in hits]}

    def read_profile(self, attribute: str | None = None) -> dict:
        out = []
        for f in self.facts:
            if attribute and f.attribute != attribute:
                continue
            v = check(f.view(), today=self.ctx.today, deadline=self.ctx.deadline)
            out.append({"id": f.id, "attribute": f.attribute, "label": f.label, "value": f.value,
                        "evidence_status": f.evidence_status, "valid_until": str(f.valid_until) if f.valid_until else None,
                        "usable_at_deadline": v.usable, "validity_note": v.reason})
        return {"facts": out}

    def compare(self, **cond) -> dict:
        try:
            r = evaluate(Condition(**cond), self.facts, self.ctx)
        except Exception as e:
            return {"error": f"invalid condition: {e}"}
        return {"status": r.status, "reason": r.reason, "fact_ids": r.facts, "reads_fields": r.params}


def run_agent(conn: psycopg.Connection, assessment_id: int, version_id: int, req: dict, facts: list[Fact], ctx: Context,
              llm: LLM, budget: Budget, b2_reason: str, escalated_because: list[str], emb: Embedder | None = None,
              rr: Reranker | None = None):
    from tenderfit.assessment.pipeline import Decision, guard

    tools = Tools(conn, version_id, facts, ctx, emb, rr)
    src = conn.execute("""SELECT p.id, p.text, p.section, p.page, coalesce(d.filename, 'document') document FROM passage p
                          JOIN document d ON d.id = p.document_id WHERE p.id = %s""", (req["passage_id"],)).fetchone()
    tools.seen[src["id"]] = dict(src)
    user = (f"Requirement ({req['obligation']}, {req['category']}): {req['statement']}\n"
            f"Equivalent allowed: {req['equivalent_allowed']}. Evidence the buyer asks for: {', '.join(req['evidence_needed']) or 'not stated'}.\n"
            f"Today {ctx.today}; submission deadline {ctx.deadline or 'not stated'}.\n"
            f"The fixed pipeline concluded: {b2_reason}\nEscalated because: {'; '.join(escalated_because)}\n\n"
            f"Source passage:\n{json.dumps(_passage_out(src))}")
    messages: list[dict] = [{"role": "user", "content": user}]
    calls = 0
    for _ in range(settings.agent_max_tool_calls + 2):
        if (why := budget.exhausted()) or calls >= settings.agent_max_tool_calls:
            reason = f"budget reached ({why or 'tool-call limit'}); not guessed"
            return Decision(NEEDS if facts else UNKNOWN, reason, "budget", reads=list(tools.seen),
                            citations=[(req["passage_id"], req["quote"])], escalate=["budget reached"]), tools
        try:
            turn = llm.tool_turn("agent", AGENT_SYSTEM, messages, TOOLS)
        except LLMError as e:
            return Decision(UNKNOWN, f"agent unavailable ({e}); not guessed", "guard", reads=list(tools.seen),
                            citations=[(req["passage_id"], req["quote"])], escalate=["model error"]), tools
        budget.spend(turn.usage.cost_usd, turn.usage.input_tokens + turn.usage.output_tokens)
        log_call(conn, "agent", AGENT_VERSION, turn.usage, {"version_id": version_id, "requirement_id": req["id"],
                                                             "assessment_id": assessment_id})
        messages.append({"role": "assistant", "content": turn.raw_content})
        if not turn.calls:
            messages.append({"role": "user", "content": "Use a tool. Finish with record_verdict or raise_question."})
            continue
        results = []
        for i, call in enumerate(turn.calls):
            usage = turn.usage if i == 0 else None
            calls += 1
            if call.name in ("record_verdict", "raise_question"):
                raw = dict(call.input)
                if call.name == "raise_question":
                    raw.update(supporting_fact_ids=[], contrary_fact_ids=[], citations=[])
                raw["conflict"] = False
                d = guard(raw, facts, ctx, req, tools.seen)
                d.decided_by = "agent" if d.decided_by == "model" else d.decided_by
                d.reads = list(tools.seen)
                if (req["passage_id"], req["quote"]) not in d.citations:
                    d.citations.insert(0, (req["passage_id"], req["quote"]))
                _step(conn, assessment_id, req["id"], calls, call.name, call.input, {"final": True}, usage)
                return d, tools
            fn = getattr(tools, call.name, None) if call.name in {t["name"] for t in TOOLS} else None
            try:
                out = fn(**call.input) if fn else {"error": f"unknown tool {call.name}"}
            except TypeError as e:
                out = {"error": f"bad arguments: {e}"}
            _step(conn, assessment_id, req["id"], calls, call.name, call.input, out, usage)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(out, default=str),
                            **({"is_error": True} if "error" in out else {})})
        messages.append({"role": "user", "content": results})
    return Decision(NEEDS if facts else UNKNOWN, "budget reached (no final answer); not guessed", "budget",
                    reads=list(tools.seen), citations=[(req["passage_id"], req["quote"])], escalate=["budget reached"]), tools


def _step(conn, assessment_id, requirement_id, step, tool, inp, out, usage) -> None:
    conn.execute(
        """INSERT INTO agent_step (assessment_id, requirement_id, step, tool, input, output, input_tokens, output_tokens, cost_usd)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (assessment_id, requirement_id, step, tool, Jsonb(inp), Jsonb(json.loads(json.dumps(out, default=str))),
         usage.input_tokens if usage else 0, usage.output_tokens if usage else 0, usage.cost_usd if usage else 0))
