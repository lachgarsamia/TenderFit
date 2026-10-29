import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from tenderfit.assessment.prompts import VERDICT_SCHEMA, VERDICT_SYSTEM, VERDICT_VERSION, verdict_user
from tenderfit.config import settings
from tenderfit.documents import references, service as docs
from tenderfit.documents.text import quote_ok
from tenderfit.extraction.extract import extract_version
from tenderfit.llm import LLM, LLMError, log_call
from tenderfit.profile.validity import check
from tenderfit.retrieval.search import Embedder, Reranker, index_version, search
from tenderfit.rules import aggregate as agg
from tenderfit.rules.engine import MET, NEEDS, NOT_MET, UNKNOWN, Condition, Context, Fact, RuleResult, evaluate

LONDON = ZoneInfo("Europe/London")
CATEGORY_FACTS = {
    "quantity": {"capacity"}, "delivery": {"capacity", "region", "service"}, "experience": {"experience"},
    "standard": {"test_report", "certificate", "product"}, "certificate": {"certificate"},
    "insurance": {"insurance"}, "financial": {"financial"}, "other": None,
}
RULE_FALLTHROUGH = ("bound ", "profile has no value", "amount not available", "no delivery region")


@dataclass
class Decision:
    status: str
    reason: str
    decided_by: str
    basis: str | None = None
    facts: list[int] = field(default_factory=list)
    citations: list[tuple[int, str]] = field(default_factory=list)
    reads: list[int] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    question: str | None = None
    needs_human: str | None = None
    escalate: list[str] = field(default_factory=list)
    cost: float = 0.0


def load_facts(conn: psycopg.Connection, profile_id: int, lot_id: int) -> list[Fact]:
    conf = {(r["fact_id"], r["kind"]) for r in conn.execute(
        "SELECT fact_id, kind FROM lot_fact_confirmation WHERE lot_id = %s AND profile_id = %s", (lot_id, profile_id))}
    return [Fact(r["id"], r["attribute"], r["label"], r["value"], r["evidence_status"], r["valid_until"],
                 r["confirm_by"], r["confirmed_at"].date() if isinstance(r["confirmed_at"], datetime) else r["confirmed_at"],
                 (r["id"], "workload") in conf, (r["id"], "declaration") in conf)
            for r in conn.execute("SELECT * FROM profile_fact WHERE profile_id = %s ORDER BY id", (profile_id,))]


def lot_context(conn: psycopg.Connection, lot_id: int, today: date) -> tuple[dict, Context]:
    row = conn.execute(
        """SELECT l.*, o.ocid, o.regions AS opp_regions, o.cpv_codes AS opp_cpv, o.current_version_id, nv.fields
           FROM lot l JOIN opportunity o ON o.id = l.opportunity_id
           JOIN notice_version nv ON nv.id = o.current_version_id WHERE l.id = %s""", (lot_id,)).fetchone()
    f = row["fields"]
    lf = next((x for x in f["lots"] if x["id"] == row["lot_no"]), {})
    deadline = datetime.fromisoformat(f["deadline"]).astimezone(LONDON).date() if f.get("deadline") else None
    fields = {"notice.deadline": deadline, "lot.quantity": float(row["quantity"]) if row["quantity"] is not None else None,
              "lot.regions": lf.get("regions") or row["opp_regions"], "lot.cpv": lf.get("cpv") or row["opp_cpv"]}
    return row, Context(today, fields)


def _fact_json(f: Fact, ctx: Context) -> dict:
    v = check(f.view(), today=ctx.today, deadline=ctx.deadline)
    return {"id": f.id, "attribute": f.attribute, "label": f.label, "value": f.value, "evidence_status": f.evidence_status,
            "valid_until": f.valid_until, "confirm_by": f.confirm_by, "usable_at_deadline": v.usable,
            "validity_note": v.reason, "confirmed_for_this_lot": f.confirmed_for_lot or f.declaration_confirmed}


def relevant_facts(facts: list[Fact], category: str) -> list[Fact]:
    want = CATEGORY_FACTS.get(category)
    return facts if want is None else [f for f in facts if f.attribute in want]


def guard(raw: dict, facts: list[Fact], ctx: Context, req: dict, passages: dict[int, dict]) -> Decision:
    """Deterministic checks on the model's verdict: rules 1, 4 and 10 are enforced here, not trusted to the model."""
    by_id = {f.id: f for f in facts}
    status, reason = raw["status"], raw["reason"].strip() or "no reason given"
    escalate = []
    citations = []
    for c in raw.get("citations", []):
        p = passages.get(c["passage_id"])
        if p and quote_ok(c["quote"], p["text"]):
            citations.append((p["id"], c["quote"]))
        else:
            escalate.append("citation rejected by verifier")
    if raw.get("conflict"):
        escalate.append("retrieved passages conflict")
    usable = lambda f: check(f.view(), today=ctx.today, deadline=ctx.deadline)  # noqa: E731
    supporting = [by_id[i] for i in raw.get("supporting_fact_ids", []) if i in by_id]
    contrary = [by_id[i] for i in raw.get("contrary_fact_ids", []) if i in by_id]
    decided_by, basis, needs_human = "model", None, None
    if status == NOT_MET:
        current = [f for f in contrary if usable(f).usable and not usable(f).stale]
        if not current:
            status = NEEDS if contrary else UNKNOWN
            reason = f"{reason} [downgraded: not met needs explicit, current contrary evidence]"
            decided_by = "guard"
        else:
            needs_human = "G6"
    elif status == MET:
        ok = [f for f in supporting if usable(f).usable and not usable(f).stale]
        proof = req["category"] in ("certificate", "insurance", "standard", "experience") or bool(req["evidence_needed"])
        if not ok:
            status, decided_by = (NEEDS if supporting else UNKNOWN), "guard"
            reason = f"{reason} [downgraded: no current supporting fact]"
        elif proof and all(f.evidence_status == "declared" and not f.declaration_confirmed for f in ok):
            status, decided_by, needs_human = NEEDS, "guard", "G2"
            reason = f"{reason} [on declared facts; proof required]"
        else:
            basis = "documents" if any(f.evidence_status == "document_on_file" for f in ok) else "declared"
    if status in (NEEDS, UNKNOWN) and raw.get("conflict"):
        needs_human = needs_human or "G5"
    used = supporting + contrary
    return Decision(status, reason, decided_by, basis, [f.id for f in used], citations, list(passages), [],
                    raw.get("question"), needs_human, escalate)


def _cache_key(version_id: int, req: dict, facts: list[Fact], ctx: Context, passages: list[int]) -> str:
    payload = [VERDICT_VERSION, version_id, req["id"], str(ctx.today), passages,
               [(f.id, f.evidence_status, str(f.valid_until), str(f.confirm_by), f.confirmed_for_lot,
                 f.declaration_confirmed, json.dumps(f.value, sort_keys=True)) for f in facts]]
    return hashlib.sha256(json.dumps(payload, default=str).encode()).hexdigest()


def decide(conn: psycopg.Connection, version_id: int, req: dict, facts: list[Fact], ctx: Context, llm: LLM,
           budget_left: float, emb: Embedder | None = None, rr: Reranker | None = None) -> Decision:
    source = conn.execute("SELECT text FROM passage WHERE id = %s", (req["passage_id"],)).fetchone()
    escalate = ["unresolved cross-reference in the source passage"] if source and references.has_unresolved_cue(source["text"]) else []
    rule: RuleResult | None = None
    if req["condition"]:
        rule = evaluate(Condition(**req["condition"]), facts, ctx, category=req["category"],
                        proof_needed=bool(req["evidence_needed"]))
        if not rule.reason.startswith(RULE_FALLTHROUGH):
            d = Decision(rule.status, rule.reason, "rule", rule.basis, rule.facts, [(req["passage_id"], req["quote"])],
                         [], rule.params, rule.question, rule.needs_human, escalate)
            if d.status == UNKNOWN:
                d.escalate.append("rule found no evidence")
            return d
    rel = relevant_facts(facts, req["category"])
    hits = search(conn, version_id, f"{req['statement']}\n{req['quote']}", k=5, emb=emb, rr=rr)
    passages = {h.id: {"id": h.id, "text": h.text, "section": h.section, "page": h.page, "document": h.document}
                for h in hits}
    if req["passage_id"] not in passages:
        src = conn.execute(
            """SELECT p.id, p.text, p.section, p.page, coalesce(d.filename, 'document') document FROM passage p
               JOIN document d ON d.id = p.document_id WHERE p.id = %s""", (req["passage_id"],)).fetchone()
        passages[src["id"]] = dict(src)
    key = _cache_key(version_id, req, rel, ctx, sorted(passages))
    cached = conn.execute("SELECT result FROM verdict_cache WHERE key = %s", (key,)).fetchone()
    cost = 0.0
    if cached:
        raw = cached["result"]
    else:
        if budget_left <= 0:
            return Decision(NEEDS if rel else UNKNOWN, "budget reached: not assessed by the model", "budget",
                            citations=[(req["passage_id"], req["quote"])], escalate=escalate + ["budget reached"])
        user = verdict_user(req, list(passages.values()), [_fact_json(f, ctx) for f in rel], str(ctx.today),
                            str(ctx.deadline) if ctx.deadline else None)
        try:
            res = llm.json("verdict", VERDICT_SYSTEM, user, VERDICT_SCHEMA)
        except LLMError as e:
            return Decision(UNKNOWN, f"model unavailable ({e}); not guessed", "guard",
                            citations=[(req["passage_id"], req["quote"])], escalate=escalate + ["model error"])
        raw, cost = res.data, res.usage.cost_usd
        log_call(conn, "verdict", VERDICT_VERSION, res.usage, {"version_id": version_id, "requirement_id": req["id"]})
        conn.execute("INSERT INTO verdict_cache (key, result) VALUES (%s, %s) ON CONFLICT DO NOTHING", (key, Jsonb(raw)))
    d = guard(raw, rel, ctx, req, passages)
    d.cost = cost
    d.escalate = escalate + d.escalate + (["model verdict unknown"] if d.status == UNKNOWN else [])
    if (req["passage_id"], req["quote"]) not in d.citations:
        d.citations.insert(0, (req["passage_id"], req["quote"]))
    if rule:
        d.params = rule.params
    return d


def _source_label(conn: psycopg.Connection, req: dict) -> str:
    r = conn.execute("""SELECT coalesce(d.filename, 'document') doc FROM passage p JOIN document d ON d.id = p.document_id
                        WHERE p.id = %s""", (req["passage_id"],)).fetchone()
    parts = [r["doc"] if r else "document"]
    if req["section"]:
        parts.append(req["section"].split(" ")[0] if req["section"][0].isdigit() else req["section"])
    if req["page"]:
        parts.append(f"p.{req['page']}")
    return " · ".join(parts)


def assess_lot(conn: psycopg.Connection, lot_id: int, profile_id: int, llm: LLM, today: date | None = None,
               http=None, emb: Embedder | None = None, rr: Reranker | None = None, mode: str = "full",
               reason: str | None = None) -> int:
    today = today or datetime.now(LONDON).date()
    lot, ctx = lot_context(conn, lot_id, today)
    version_id = lot["current_version_id"]
    docs.process_version(conn, version_id, http)
    index_version(conn, version_id, emb)
    extract_version(conn, version_id, llm)
    inv = docs.inventory(conn, version_id)
    conn.execute("UPDATE assessment SET superseded = true WHERE lot_id = %s AND profile_id = %s AND NOT superseded",
                 (lot_id, profile_id))
    aid = conn.execute(
        """INSERT INTO assessment (profile_id, lot_id, version_id, coverage, provisional, mode, full_reason, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'running') RETURNING id""",
        (profile_id, lot_id, version_id, inv["coverage"], inv["coverage"] != "complete", mode, reason)).fetchone()["id"]
    conn.commit()
    facts = load_facts(conn, profile_id, lot_id)
    reqs = conn.execute(
        """SELECT * FROM requirement WHERE version_id = %s AND (lot_no IS NULL OR lot_no = %s) ORDER BY id""",
        (version_id, lot["lot_no"])).fetchall()
    spent, calls, budget_hit, vs = 0.0, 0, False, []
    for req in reqs:
        d = decide(conn, version_id, req, facts, ctx, llm, settings.lot_cost_cap_usd - spent, emb, rr)
        spent += d.cost
        calls += d.cost > 0
        budget_hit |= d.decided_by == "budget"
        vid = conn.execute(
            """INSERT INTO verdict (assessment_id, requirement_id, status, reason, basis, needs_human, provisional,
                                    question, escalate, escalate_reason, decided_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (aid, req["id"], d.status, d.reason, d.basis, d.needs_human, inv["coverage"] != "complete", d.question,
             bool(d.escalate), "; ".join(dict.fromkeys(d.escalate)) or None, d.decided_by)).fetchone()["id"]
        for pid, quote in dict.fromkeys(d.citations):
            conn.execute("INSERT INTO verdict_citation (verdict_id, passage_id, quote) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                         (vid, pid, quote))
        for fid in dict.fromkeys(d.facts):
            conn.execute("INSERT INTO verdict_fact (verdict_id, fact_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (vid, fid))
            conn.execute("""INSERT INTO dependency (requirement_id, version_id, kind, target_type, target_id, recorded_by, verdict_id)
                            VALUES (%s, %s, 'fact', 'fact', %s, 'verdict', %s)""", (req["id"], version_id, str(fid), vid))
        for pid in d.reads:
            if pid != req["passage_id"]:
                conn.execute("""INSERT INTO dependency (requirement_id, version_id, kind, target_type, target_id, recorded_by, verdict_id)
                                VALUES (%s, %s, 'read', 'passage', %s, 'verdict', %s)""", (req["id"], version_id, str(pid), vid))
        if d.question:
            conn.execute("INSERT INTO question (verdict_id, text) VALUES (%s, %s)", (vid, d.question))
        vs.append(agg.V(req["id"], req["statement"], req["obligation"], req["category"], d.status, d.reason,
                        _source_label(conn, req), d.question, d.basis))
    refs = [agg.Ref(r["label"], r["kind"], r["essential"], r["status"]) for r in inv["documents"]]
    product_cpv = [c for f in facts if f.attribute == "product" for c in f.value.get("cpv", [])]
    region = next((f.value.get("itl", []) for f in facts if f.attribute == "region"), [])
    rel = agg.relevance(ctx.fields["lot.cpv"] or [], product_cpv, ctx.fields["lot.regions"] or [], region)
    dims = agg.dimensions(vs, refs, rel, inv["coverage"])
    conn.execute(
        """UPDATE assessment SET recommendation = %s, dimensions = %s, provisional = %s, status = 'done', cost_usd = %s,
                  model_calls = %s, budget_reached = %s, finished_at = now() WHERE id = %s""",
        (dims.recommendation, Jsonb(dims.__dict__), dims.provisional, spent, calls, budget_hit, aid))
    conn.commit()
    return aid
