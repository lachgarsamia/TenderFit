import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from tenderfit.assessment.config import BASELINES, PipelineConfig
from tenderfit.assessment.confirm import overrides
from tenderfit.assessment.contradiction import find_conflicts
from tenderfit.assessment.prompts import VERDICT_SCHEMA, VERDICT_SYSTEM, VERDICT_VERSION, verdict_user
from tenderfit.config import settings
from tenderfit.db import Conn, must
from tenderfit.documents import references
from tenderfit.documents import service as docs
from tenderfit.documents.text import normalise, quote_ok
from tenderfit.extraction.extract import extract_version
from tenderfit.llm import LLM, LLMError, log_call
from tenderfit.profile.validity import check
from tenderfit.retrieval import graph, versioned
from tenderfit.retrieval.search import Embedder, Reranker, corrective_query, index_version, search
from tenderfit.rules import aggregate as agg
from tenderfit.rules.engine import MET, NEEDS, NOT_MET, UNKNOWN, Condition, Context, Fact, Outcome, RuleResult, evaluate

LONDON = ZoneInfo("Europe/London")
CATEGORY_FACTS = {
    "quantity": {"capacity"}, "delivery": {"capacity", "region", "service"}, "experience": {"experience"},
    "standard": {"test_report", "certificate", "product"}, "certificate": {"certificate"},
    "insurance": {"insurance"}, "financial": {"financial"}, "other": None,
}


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
    tokens: int = 0
    documents: list[int] = field(default_factory=list)
    recorded_by: str = "verdict"
    required: str | None = None
    observed: str | None = None
    conditions: list[dict] = field(default_factory=list)
    retrieval: list[dict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    conflict: bool = False


def load_facts(conn: Conn, profile_id: int, lot_id: int, version_id: int | None = None) -> list[Fact]:
    """A confirmation for this lot holds for the notice version it was given on and for the fact as it then stood."""
    conf = {(r["fact_id"], r["kind"]) for r in conn.execute(
        """SELECT c.fact_id, c.kind FROM lot_fact_confirmation c JOIN profile_fact f ON f.id = c.fact_id
           WHERE c.lot_id = %s AND c.profile_id = %s AND (c.version_id IS NULL OR c.version_id = %s)
             AND c.created_at >= f.updated_at""", (lot_id, profile_id, version_id))}
    return [Fact(r["id"], r["attribute"], r["label"], r["value"], r["evidence_status"], r["valid_until"],
                 r["confirm_by"], r["confirmed_at"].date() if isinstance(r["confirmed_at"], datetime) else r["confirmed_at"],
                 (r["id"], "workload") in conf, (r["id"], "declaration") in conf)
            for r in conn.execute("SELECT * FROM profile_fact WHERE profile_id = %s ORDER BY id", (profile_id,))]


def lot_context(conn: Conn, lot_id: int, today: date, snapshot: int | None = None, *, frozen: bool = False) -> tuple[dict, Context]:
    row = must(conn.execute(
        """SELECT l.*, o.ocid, o.regions AS opp_regions, o.cpv_codes AS opp_cpv, o.current_version_id, nv.fields
           FROM lot l JOIN opportunity o ON o.id = l.opportunity_id
           JOIN notice_version nv ON nv.id = coalesce(%s, o.current_version_id) AND nv.opportunity_id = o.id WHERE l.id = %s""", (snapshot, lot_id)).fetchone())
    f = row["fields"]
    lf: dict = next((x for x in f["lots"] if x["id"] == row["lot_no"]), {})
    if frozen or snapshot is not None:
        if not lf:
            raise ValueError("lot missing from requested notice version")
        row.update(quantity=lf.get("quantity"), opp_cpv=f.get("cpv", []), opp_regions=f.get("regions", []))
    deadline = datetime.fromisoformat(f["deadline"]).astimezone(LONDON).date() if f.get("deadline") else None
    period = lf.get("contract_period") or {}
    contract_end = period.get("endDate")
    fields = {"notice.deadline": deadline, "lot.quantity": float(row["quantity"]) if row["quantity"] is not None else None,
              "lot.regions": lf.get("regions") or row["opp_regions"], "lot.cpv": lf.get("cpv") or row["opp_cpv"],
              "lot.contract_end": date.fromisoformat(contract_end[:10]) if contract_end else None}
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
    if status == MET and any("citation rejected" in e for e in escalate):
        # A verdict whose reasoning cited text that is not in the source is not proven, whatever it concludes.
        status, decided_by, basis, needs_human = NEEDS, "guard", None, needs_human or "G2"
        reason = f"{reason} [downgraded: a quote it relied on was not found in the source]"
    if status in (NEEDS, UNKNOWN) and raw.get("conflict"):
        needs_human = needs_human or "G5"
    used = supporting + contrary
    return Decision(status, reason, decided_by, basis, [f.id for f in used], citations, [pid for pid, _ in citations], [],
                    raw.get("question"), needs_human, escalate)


def unguarded(raw: dict, facts: list[Fact], passages: dict[int, dict]) -> Decision:
    """B1: the model's verdict as given; citations kept without verification."""
    ids = {f.id for f in facts}
    used = [i for i in raw.get("supporting_fact_ids", []) + raw.get("contrary_fact_ids", []) if i in ids]
    cites = [(c["passage_id"], c["quote"]) for c in raw.get("citations", []) if c["passage_id"] in passages]
    return Decision(raw["status"], raw["reason"], "model", None, used, cites, [p for p, _ in cites], [], raw.get("question"))


def _cache_key(version_id: int, req: dict, facts: list[Fact], ctx: Context, passages: list, profile_id: int | None) -> str:
    payload = [VERDICT_VERSION, profile_id, version_id, req["id"], str(ctx.today), str(ctx.deadline), passages,
               [(f.id, f.evidence_status, str(f.valid_until), str(f.confirm_by), f.confirmed_for_lot,
                 f.declaration_confirmed, json.dumps(f.value, sort_keys=True)) for f in facts]]
    return hashlib.sha256(json.dumps(payload, default=str).encode()).hexdigest()


@dataclass
class ConditionResult:
    id: int | None
    label: str
    mandatory: bool
    rule: RuleResult | None  # None: this condition must be interpreted


def _rule_outcomes(conn: Conn, req: dict, facts: list[Fact], ctx: Context, config: PipelineConfig) -> list[ConditionResult]:
    rows = conn.execute("SELECT * FROM requirement_condition WHERE requirement_id = %s ORDER BY ordinal", (req["id"],)).fetchall()
    if not rows:  # requirements extracted before conditions existed
        rows = [{"id": None, "label": req["statement"], "mandatory": True, "condition": req["condition"], "quote": req["quote"]}]
    out = []
    for row in rows:
        rule = None
        if row["condition"] and config.rules:
            try:
                cond = Condition(**row["condition"])
            except ValidationError:
                cond = None
            if cond:
                rule = evaluate(cond, facts, ctx, category=req["category"], proof_needed=bool(req["evidence_needed"]),
                                requirement=f"{req['statement']} {row['quote'] or req['quote']}",
                                equivalent_allowed=req["equivalent_allowed"])
                if rule.outcome is Outcome.NEEDS_MODEL:
                    rule = None
        out.append(ConditionResult(row["id"], row["label"], row["mandatory"], rule))
    return out


def parent_status(statuses: list[str]) -> str:
    """A requirement is met only if every mandatory part is. One definite failure decides it; otherwise the weakest part does."""
    if NOT_MET in statuses:
        return NOT_MET
    if statuses and all(s == MET for s in statuses):
        return MET
    return NEEDS if NEEDS in statuses or MET in statuses else UNKNOWN


def _settled(results: list[ConditionResult], mandatory_only: bool = False) -> list[tuple[ConditionResult, RuleResult]]:
    return [(c, c.rule) for c in results if c.rule and (c.mandatory or not mandatory_only)]


def _from_rules(req: dict, results: list[ConditionResult], escalate: list[str]) -> Decision:
    settled = _settled(results)
    binding = _settled(results, mandatory_only=True) or settled
    status = parent_status([rule.status for _, rule in binding])
    if len(settled) == 1:
        r = settled[0][1]
        reason = r.reason
    else:
        reason = "; ".join(f"{c.label}: {rule.reason}" for c, rule in settled)
        r = next((rule for _, rule in binding if rule.status == status), binding[0][1])
    bases = {rule.basis for _, rule in binding}
    basis = "documents" if bases == {"documents"} else ("declared" if "declared" in bases or "documents" in bases else "rule")
    return Decision(status, reason, "rule", basis if status == MET else r.basis,
                    list(dict.fromkeys(f for _, rule in settled for f in rule.facts)), [(req["passage_id"], req["quote"])], [],
                    list(dict.fromkeys(p for _, rule in settled for p in rule.params)),
                    next((rule.question for _, rule in binding if rule.status == status and rule.question), None),
                    next((rule.needs_human for _, rule in binding if rule.status == status and rule.needs_human), None),
                    list(escalate), required=r.required, observed=r.observed)


def _lineage(conn: Conn, req: dict) -> list[int]:
    """This requirement and its predecessors while the source wording stayed the same: decisions made on them still apply."""
    ids, wording = [req["id"]], normalise(req["quote"])
    parent = req.get("carried_from")
    while parent:
        row = conn.execute("SELECT id, quote, carried_from FROM requirement WHERE id = %s", (parent,)).fetchone()
        if not row or normalise(row["quote"]) != wording:
            break
        ids.append(row["id"])
        parent = row["carried_from"]
    return ids


def _combine(d: Decision, results: list[ConditionResult]) -> Decision:
    """Rules bind the model: its reading of the open conditions can never lift a condition a rule did not accept."""
    settled = _settled(results, mandatory_only=True)
    if not settled:
        return d
    status = parent_status([d.status] + [rule.status for _, rule in settled])
    d.facts = list(dict.fromkeys(d.facts + [f for _, rule in _settled(results) for f in rule.facts]))
    if status != d.status:
        worst, why = next((pair for pair in settled if pair[1].status in (status, NOT_MET)), settled[0])
        d.reason = f"{d.reason}; {worst.label}: {why.reason}"
        d.status, d.needs_human, d.question = status, why.needs_human or d.needs_human, why.question or d.question
        d.basis = why.basis if status != MET else d.basis
        d.required, d.observed = why.required, why.observed
    return d


def _model_decision(conn: Conn, version_id: int, req: dict, facts: list[Fact], ctx: Context, llm: LLM, budget_left: float,
                    emb, rr, config: PipelineConfig, profile_id: int | None, escalate: list[str], labels: set[str],
                    open_conditions: list[str], paths: versioned.EvidencePaths | None = None) -> Decision:
    rel = relevant_facts(facts, req["category"]) if config.rules else facts
    answers = [r["answer"] for r in conn.execute(
        """SELECT answer FROM question WHERE requirement_id = ANY(%s) AND answer IS NOT NULL
           AND profile_id IS NOT DISTINCT FROM %s ORDER BY answered_at""", (_lineage(conn, req), profile_id))]
    if not rel and config.corrective and not answers:
        return Decision(UNKNOWN, "no fact in your profile bears on this, so it was not sent for interpretation", "guard",
                        citations=[(req["passage_id"], req["quote"])], escalate=escalate + ["no relevant fact"],
                        question=f"Do you have evidence for: {req['statement']}?")
    links = paths.links if paths is not None else graph.expand(conn, req["passage_id"], labels, profile_id) if config.graph else []
    query = f"{req['statement']}\n{req['quote']}"
    hits = search(conn, version_id, query, k=5, emb=emb, rr=rr, hybrid=config.hybrid, profile_id=profile_id, graph_links=links)
    floor = settings.retrieval_min_score
    if floor is not None:
        hits = [h for h in hits if h.score >= floor or h.id == req["passage_id"] or h.id in {g["id"] for g in links}]
    gaps = graph.reference_gaps(conn, version_id, req["passage_id"], labels, profile_id) if config.corrective else []
    corrected = 0
    while config.corrective and corrected < settings.corrective_max_attempts and (gaps or (floor is not None and len(hits) <= 1)):
        corrected += 1
        more = search(conn, version_id, corrective_query(req, gaps), k=5, emb=emb, rr=rr, hybrid=config.hybrid, profile_id=profile_id,
                      exclude={h.id for h in hits})
        wanted = [g.lower() for g in gaps]
        for h in more:
            if (floor is None or h.score >= floor) and (not wanted or any(g in h.text.lower() for g in wanted)):
                h.why.insert(0, "second search for what the first did not find")
                hits.append(h)
    passages = {h.id: {"id": h.id, "text": h.text, "section": h.section, "page": h.page, "document": h.document} for h in hits}
    retrieval = [{"passage_id": h.id, "document": h.document, "section": h.section, "page": h.page, "why": h.why} for h in hits]
    if req["passage_id"] not in passages:
        src = must(conn.execute(
            """SELECT p.id, p.text, p.section, p.page, coalesce(d.filename, 'document') document FROM passage p
               JOIN document d ON d.id = p.document_id WHERE p.id = %s""", (req["passage_id"],)).fetchone())
        passages[src["id"]] = dict(src)
    retrieval.append({"passage_id": req["passage_id"], "why": ["the requirement's own source"]})
    key = _cache_key(version_id, req, rel, ctx, sorted(passages) + answers + [config.name] + open_conditions, profile_id)
    cached = conn.execute("SELECT result FROM verdict_cache WHERE key = %s", (key,)).fetchone()
    cost, tokens = 0.0, 0
    if cached:
        raw = cached["result"]
    else:
        if budget_left <= 0:
            return Decision(NEEDS if rel else UNKNOWN, "budget reached: not assessed by the model", "budget", retrieval=retrieval,
                            citations=[(req["passage_id"], req["quote"])], escalate=escalate + ["budget reached"])
        user = verdict_user(req, list(passages.values()), [_fact_json(f, ctx) for f in rel], str(ctx.today),
                            str(ctx.deadline) if ctx.deadline else None, answers, open_conditions)
        try:
            res = llm.json("verdict", VERDICT_SYSTEM, user, VERDICT_SCHEMA)
        except LLMError as e:
            return Decision(UNKNOWN, f"model unavailable ({e}); not guessed", "guard", retrieval=retrieval,
                            citations=[(req["passage_id"], req["quote"])], escalate=escalate + ["model error"])
        raw, cost, tokens = res.data, res.usage.cost_usd, res.usage.input_tokens + res.usage.output_tokens
        log_call(conn, "verdict", VERDICT_VERSION, res.usage, {"version_id": version_id, "requirement_id": req["id"], "profile_id": profile_id})
        conn.execute("INSERT INTO verdict_cache (key, result) VALUES (%s, %s) ON CONFLICT DO NOTHING", (key, Jsonb(raw)))
    d = guard(raw, rel, ctx, req, passages) if config.guard else unguarded(raw, rel, passages)
    d.cost, d.tokens, d.retrieval = cost, tokens, retrieval
    d.conflict = bool(raw.get("conflict"))
    d.escalate = escalate + d.escalate + (["model verdict unknown"] if d.status == UNKNOWN else [])
    if (req["passage_id"], req["quote"]) not in d.citations:
        d.citations.insert(0, (req["passage_id"], req["quote"]))
    if answers and d.status == UNKNOWN:
        d.status, d.question = NEEDS, None
        d.reason = f"{d.reason} You answered: \"{answers[-1][:200]}\". This is on record as a declaration; proof may still be needed."
    return d


def decide(conn: Conn, version_id: int, req: dict, facts: list[Fact], ctx: Context, llm: LLM,
           budget_left: float, emb: Embedder | None = None, rr: Reranker | None = None,
           config: PipelineConfig = PipelineConfig("B2"), profile_id: int | None = None) -> Decision:
    source = conn.execute("SELECT text FROM passage WHERE id = %s", (req["passage_id"],)).fetchone()
    # A reference in this requirement's own words, not any reference elsewhere in a long passage.
    labels = {r.label for r in references.find(req["quote"]) if r.kind != "named"} | set(req.get("refs") or []) | set(req.get("defined_terms") or [])
    escalate = ["unresolved cross-reference in the source passage"] if source and references.has_unresolved_cue(req["quote"]) else []
    paths = versioned.resolve(conn, version_id, req["passage_id"], labels, profile_id) if config.versionrag else None
    results = _rule_outcomes(conn, req, facts, ctx, config)
    open_results = [c for c in results if not c.rule and c.mandatory]
    if any(c.rule for c in results) and not open_results:
        d = _from_rules(req, results, escalate)
        answered = conn.execute(
            """SELECT answer FROM question WHERE requirement_id = ANY(%s) AND answer IS NOT NULL AND profile_id IS NOT DISTINCT FROM %s
               ORDER BY answered_at DESC LIMIT 1""", (_lineage(conn, req), profile_id)).fetchone()
        if d.status == UNKNOWN:
            d.escalate.append("rule found no evidence")
            if answered:  # the answer is on record: do not ask again, and do not treat it as proof
                d.status, d.question = NEEDS, None
                d.reason = f"{d.reason}. You answered: \"{answered['answer'][:200]}\". This is on record as a declaration; add a document to prove it."
    else:
        labels_open = [c.label for c in open_results] if len(results) > 1 else []
        d = _model_decision(conn, version_id, req, facts, ctx, llm, budget_left, emb, rr, config, profile_id, escalate, labels, labels_open, paths)
        d = _combine(d, results)
    if config.rules:
        d.params = list(dict.fromkeys(d.params + [p for c in results if c.rule for p in c.rule.params]))
    d.conditions = [{"id": c.id, "label": c.label, "mandatory": c.mandatory, "status": c.rule.status if c.rule else d.status,
                     "reason": c.rule.reason if c.rule else d.reason, "decided_by": "rule" if c.rule else d.decided_by,
                     "required": c.rule.required if c.rule else None, "observed": c.rule.observed if c.rule else None}
                    for c in results if c.id is not None]
    if config.corrective:
        _apply_gaps(conn, version_id, req, d, labels, profile_id)
        _apply_conflicts(conn, version_id, req, d, profile_id)
    if paths is not None:
        d.reads.extend(entry["passage_id"] for entry in d.retrieval)
        d.reads.extend(link["id"] for link in paths.links)
        d.retrieval.extend({"passage_id": link["id"], "path": link["path"], "why": [" → ".join(link["steps"])]}
                           for link in paths.links)
        d.retrieval.append({"passage_id": req["passage_id"], "why": ["requirement source"]})
        d.retrieval = versioned.provenance(conn, version_id, profile_id, d.retrieval)
        d.gaps = sorted(set(d.gaps + paths.gaps))
        if paths.gaps and d.status == MET:
            d.status, d.basis, d.needs_human = NEEDS, None, "G5"
            d.reason += ". Evidence needs review: " + ", ".join(paths.gaps)
            d.question = "Which passage governs " + ", ".join(paths.gaps) + "?"
    return d


def _apply_gaps(conn: Conn, version_id: int, req: dict, d: Decision, labels: set[str], profile_id: int | None) -> None:
    """A requirement that points somewhere we could not read cannot be called met on what we did read."""
    d.gaps = graph.reference_gaps(conn, version_id, req["passage_id"], {r.label for r in references.find(req["quote"]) if r.kind != "named"},
                                  profile_id)
    if d.gaps and d.status == MET:
        names = ", ".join(d.gaps)
        d.status, d.basis = NEEDS, None
        d.needs_human = d.needs_human or "G5"
        d.question = d.question or f"This requirement refers to {names}, which is not in the documents read. Upload it, or confirm the buyer issued none."
        d.reason = f"{d.reason.rstrip('. ')}; but it refers to {names}, which could not be read"


def _apply_conflicts(conn: Conn, version_id: int, req: dict, d: Decision, profile_id: int | None) -> None:
    """Two documents stating the same obligation with different numbers: the supplier must ask the buyer which one holds."""
    conflicts = find_conflicts(conn, version_id, req["passage_id"], req["quote"], profile_id)
    if not conflicts:
        return
    c = conflicts[0]
    d.conflict = True
    d.citations += [(x["passage_id"], x["quote"]) for x in conflicts[:3]]
    d.reads += [x["passage_id"] for x in conflicts[:3]]
    d.status, d.basis, d.needs_human = NEEDS, None, "G5"
    d.question = f"The documents disagree: \"{req['quote']}\" and \"{c['quote']}\" ({c['document']}). Which does the buyer require?"
    d.reason = f"{d.reason.rstrip('. ')}; the documents state this differently (\"{c['quote']}\" in {c['document']})"
    d.escalate.append("documents disagree")


def evidence_quality(d: Decision, facts: list[Fact]) -> tuple[str, str]:
    """Reasons, not probabilities: what kind of evidence this verdict rests on, and what remains uncertain."""
    used = [f for f in facts if f.id in d.facts]
    if d.conflict:
        return "ambiguous", "The documents or the facts disagree, so this needs a person or the buyer to settle it."
    if d.gaps:
        return "ambiguous", f"It refers to {', '.join(d.gaps)}, which could not be read."
    if d.status == NOT_MET:
        return "contradictory", "A current fact in your profile contradicts this requirement."
    if d.decided_by == "budget":
        return "missing", "The cost limit for this lot was reached before this was examined."
    if d.status == MET:
        if any(f.evidence_status == "document_on_file" for f in used) and d.basis == "documents":
            return "strong", "Backed by a document on file whose quoted text was found in the file."
        return "weak", "Rests on what you declared. No document on file proves it."
    if d.status == NEEDS:
        return ("weak", "Something supports this, but it must be confirmed before you rely on it.") if used else (
            "ambiguous", "The text can be read in more than one way, or a person must confirm it.")
    return "missing", "Nothing in your profile or in the documents read settles this."


def _source_label(conn: Conn, req: dict) -> str:
    r = conn.execute("""SELECT coalesce(d.filename, 'document') doc FROM passage p JOIN document d ON d.id = p.document_id
                        WHERE p.id = %s""", (req["passage_id"],)).fetchone()
    parts = [r["doc"] if r else "document"]
    if req["section"]:
        parts.append(req["section"].split(" ")[0] if req["section"][0].isdigit() else req["section"])
    if req["page"]:
        parts.append(f"p.{req['page']}")
    return " · ".join(parts)


def _overridden(req: dict, overridden: dict, lineage: list[int]) -> dict | None:
    return next((overridden[i] for i in lineage if i in overridden), None)


def _carry_verdict(conn: Conn, aid: int, req: dict, old_vid: int, pmap: dict[int, int],
                   provisional: bool, profile_id: int, version_id: int, versionrag: bool = False) -> dict:
    old = must(conn.execute("SELECT * FROM verdict WHERE id = %s", (old_vid,)).fetchone())
    retrieval = [{**r, "passage_id": pmap.get(r.get("passage_id"), r.get("passage_id"))} for r in old["retrieval"]]
    if versionrag:
        for entry in retrieval:
            if "path" in entry:
                entry["path"] = [pmap.get(pid, pid) for pid in entry["path"]]
        retrieval = versioned.provenance(conn, version_id, profile_id, retrieval)
    vid = must(conn.execute(
        """INSERT INTO verdict (assessment_id, requirement_id, status, reason, basis, needs_human, provisional, question,
                                escalate, escalate_reason, decided_by, carried_over, override_status, override_reason, override_by,
                                required, observed, evidence_quality, uncertainty, retrieval, reference_gaps)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (aid, req["id"], old["status"], old["reason"], old["basis"], old["needs_human"], provisional, old["question"],
         old["escalate"], old["escalate_reason"], old["decided_by"], old["override_status"], old["override_reason"], old["override_by"],
         old["required"], old["observed"], old["evidence_quality"], old["uncertainty"], Jsonb(retrieval), old["reference_gaps"])).fetchone())["id"]
    for c in conn.execute("SELECT passage_id, quote FROM verdict_citation WHERE verdict_id = %s", (old_vid,)).fetchall():
        conn.execute("INSERT INTO verdict_citation (verdict_id, passage_id, quote) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                     (vid, pmap.get(c["passage_id"], c["passage_id"]), c["quote"]))
    for f in conn.execute("SELECT fact_id FROM verdict_fact WHERE verdict_id = %s", (old_vid,)).fetchall():
        conn.execute("INSERT INTO verdict_fact (verdict_id, fact_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (vid, f["fact_id"]))
    conn.execute(
        """INSERT INTO verdict_condition (verdict_id, condition_id, status, reason, decided_by, required, observed)
           SELECT %s, new.id, vc.status, vc.reason, vc.decided_by, vc.required, vc.observed
           FROM verdict_condition vc JOIN requirement_condition oc ON oc.id = vc.condition_id
           JOIN requirement_condition new ON new.requirement_id = %s AND new.ordinal = oc.ordinal
           WHERE vc.verdict_id = %s""", (vid, req["id"], old_vid))
    # Everything the kept verdict depended on stays recorded, so the next amendment can still reach it.
    refs_new = {r["label"].lower(): r["id"] for r in conn.execute("SELECT id, label FROM document_ref WHERE version_id = %s", (version_id,))}
    for dep in conn.execute("SELECT * FROM dependency WHERE verdict_id = %s", (old_vid,)).fetchall():
        target = dep["target_id"]
        if dep["target_type"] == "passage":
            target = str(pmap.get(int(target), target))
        elif dep["target_type"] == "document":
            label = conn.execute("SELECT lower(label) l FROM document_ref WHERE id = %s", (int(target),)).fetchone()
            target = str(refs_new.get(label["l"], target)) if label else target
        conn.execute("""INSERT INTO dependency (requirement_id, version_id, kind, target_type, target_id, recorded_by, verdict_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                     (req["id"], version_id, dep["kind"], dep["target_type"], target, dep["recorded_by"], vid))
    if old["question"]:
        prior = must(conn.execute("SELECT answer, answered_by, answered_at FROM question WHERE verdict_id = %s ORDER BY id LIMIT 1", (old_vid,)).fetchone())
        conn.execute("""INSERT INTO question (verdict_id, requirement_id, text, profile_id, answer, answered_by, answered_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                     (vid, req["id"], old["question"], profile_id, prior and prior["answer"], prior and prior["answered_by"],
                      prior and prior["answered_at"]))
    return {**old, "id": vid}


STALE_RUNNING_MINUTES = 90


class AssessmentFailed(Exception):
    """Raised when a run could not finish on its current attempt; the last good assessment stays current."""


def _begin(conn: Conn, lot_id: int, profile_id: int, version_id: int, mode: str, reason: str | None,
           shadow: bool, job_id: int | None) -> int:
    """Open a running assessment without touching the current completed one. An older unfinished run is closed."""
    if not shadow:
        conn.execute(
            """UPDATE assessment SET status = 'failed', superseded = true, finished_at = now(), error_stage = 'interrupted',
                      error = 'interrupted: a new attempt started before this run finished'
               WHERE lot_id = %s AND profile_id = %s AND status = 'running' AND NOT shadow""", (lot_id, profile_id))
    aid = must(conn.execute(
        """INSERT INTO assessment (profile_id, lot_id, version_id, provisional, mode, full_reason, status, shadow, superseded,
                                   job_id, progress)
           VALUES (%s, %s, %s, true, %s, %s, 'running', %s, %s, %s, %s) RETURNING id""",
        (profile_id, lot_id, version_id, mode, reason, shadow, shadow, job_id, Jsonb({"stage": "documents"}))).fetchone())["id"]
    conn.commit()
    return aid


def _progress(conn: Conn, aid: int, state: dict, **values) -> None:
    state.update(values)
    conn.execute("UPDATE assessment SET progress = %s WHERE id = %s", (Jsonb(state), aid))
    conn.commit()


def _mark_failed(conn: Conn, aid: int, stage: dict, error: Exception) -> None:
    conn.rollback()
    conn.execute(
        """UPDATE assessment SET status = 'failed', superseded = true, finished_at = now(), error = %s, error_stage = %s
           WHERE id = %s AND status = 'running'""",
        (f"{type(error).__name__}: {error}"[:2000], stage.get("stage", "unknown"), aid))
    conn.commit()


def recover_stale_assessments(conn: Conn, minutes: int = STALE_RUNNING_MINUTES) -> int:
    """A worker that died cannot mark its own run failed; close runs with no live job after a time limit."""
    n = conn.execute(
        """UPDATE assessment a SET status = 'failed', superseded = true, finished_at = now(), error_stage = 'interrupted',
                  error = 'interrupted: the worker stopped before this run finished'
           WHERE a.status = 'running' AND a.started_at < now() - make_interval(mins => %s)
             AND NOT EXISTS (SELECT 1 FROM job j WHERE j.id = a.job_id AND j.status = 'running'
                             AND j.locked_at > now() - make_interval(mins => %s))""", (minutes, minutes)).rowcount
    conn.commit()
    return n


def assess_lot(conn: Conn, lot_id: int, profile_id: int, llm: LLM, today: date | None = None,
               http=None, emb: Embedder | None = None, rr: Reranker | None = None, mode: str = "full",
               reason: str | None = None, version_id: int | None = None, carry: dict[int, int] | None = None,
               pmap: dict[int, int] | None = None, shadow: bool = False, agent: bool | None = None,
               config: PipelineConfig | None = None, job_id: int | None = None,
               finalise: Callable[[int], None] | None = None) -> int:
    """carry maps requirement ids of this version to verdict ids to copy unchanged (targeted reassessment).

    The previous completed assessment stays current until this run commits. Any failure is recorded on this run
    and raised again, so the job queue can retry it. `finalise` runs inside the final transaction, so records that
    describe this run (such as a change impact) become visible exactly when the run does."""
    today = today or datetime.now(LONDON).date()
    if config is None and settings.versionrag_enabled:
        config = BASELINES["VersionRAG"]
    lot, ctx = lot_context(conn, lot_id, today, version_id if config and config.versionrag else None,
                           frozen=bool(config and config.versionrag))
    version_id = version_id or lot["current_version_id"]
    aid = _begin(conn, lot_id, profile_id, version_id, mode, reason, shadow, job_id)
    stage: dict = {"stage": "documents"}
    try:
        _run(conn, aid, lot, ctx, profile_id, llm, http, emb, rr, version_id, carry or {}, pmap or {}, shadow, agent, config, stage,
             finalise)
    except Exception as e:
        _mark_failed(conn, aid, stage, e)
        raise
    return aid


def _record_verdict_links(conn: Conn, vid: int, req: dict, version_id: int, d: Decision, facts: list[Fact],
                          profile_id: int) -> None:
    """Citations, facts, conditions and every dependency, each tied to this verdict and so to this supplier."""
    def dep(kind: str, target_type: str, target: object, recorded_by: str) -> None:
        conn.execute("""INSERT INTO dependency (requirement_id, version_id, kind, target_type, target_id, recorded_by, verdict_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)""", (req["id"], version_id, kind, target_type, str(target), recorded_by, vid))

    for pid, quote in dict.fromkeys(d.citations):
        conn.execute("INSERT INTO verdict_citation (verdict_id, passage_id, quote) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                     (vid, pid, quote))
    for fid in dict.fromkeys(d.facts):
        conn.execute("INSERT INTO verdict_fact (verdict_id, fact_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (vid, fid))
        dep("fact", "fact", fid, "verdict")
    for c in d.conditions:
        conn.execute("""INSERT INTO verdict_condition (verdict_id, condition_id, status, reason, decided_by, required, observed)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                     (vid, c["id"], c["status"], c["reason"], c["decided_by"], c["required"], c["observed"]))
    params = set(d.params)
    # Validity is measured at the deadline, so a dated fact makes this verdict depend on the deadline even if no condition names it.
    if any(f.id in d.facts and f.valid_until for f in facts):
        params.add("notice.deadline")
    for p in sorted(params):
        dep("parameter", "field", p, "rule_engine")
    for pid in dict.fromkeys(d.reads):
        if pid != req["passage_id"]:
            dep("read", "passage", pid, d.recorded_by)
    for doc_ref in d.documents:
        dep("read", "document", doc_ref, d.recorded_by)
    if d.question:
        conn.execute("INSERT INTO question (verdict_id, requirement_id, text, profile_id) VALUES (%s, %s, %s, %s)",
                     (vid, req["id"], d.question, profile_id))


def _run(conn: Conn, aid: int, lot: dict, ctx: Context, profile_id: int, llm: LLM, http, emb, rr, version_id: int,
         carry: dict[int, int], pmap: dict[int, int], shadow: bool, agent: bool | None, config: PipelineConfig | None,
         stage: dict, finalise: Callable[[int], None] | None) -> None:
    lot_id = lot["id"]
    docs.process_version(conn, version_id, http)
    _progress(conn, aid, stage, stage="embedding")
    index_version(conn, version_id, emb)
    graph.build_graph(conn, version_id)
    _progress(conn, aid, stage, stage="extraction")
    extract_version(conn, version_id, llm, profile_id=profile_id)
    inv = docs.inventory(conn, version_id, profile_id)
    conn.execute("UPDATE assessment SET coverage = %s, provisional = %s WHERE id = %s",
                 (inv["coverage"], inv["coverage"] != "complete", aid))
    facts = load_facts(conn, profile_id, lot_id, version_id)
    reqs = conn.execute(
        """SELECT * FROM requirement WHERE version_id = %s AND (lot_no IS NULL OR lot_no = %s)
             AND (owner_profile_id IS NULL OR owner_profile_id = %s)
           ORDER BY obligation = 'mandatory' DESC, id""",
        (version_id, lot["lot_no"], profile_id)).fetchall()
    from tenderfit.agent.loop import Budget, run_agent

    budget, calls, budget_hit, vs = Budget(), 0, False, []
    config = config or PipelineConfig("B3" if (settings.agent_enabled if agent is None else agent) else "B2",
                                      agent=settings.agent_enabled if agent is None else agent)
    use_agent = config.agent
    overridden = overrides(conn, profile_id)
    _progress(conn, aid, stage, stage="assessment", done=0, total=len(reqs))
    for done, req in enumerate(reqs, start=1):
        previous = carry.get(req["id"])
        if previous and config.versionrag and not versioned.reusable(conn, previous, req, version_id, profile_id, pmap):
            previous = None
        if previous and config.versionrag and pmap and find_conflicts(conn, version_id, req["passage_id"], req["quote"], profile_id):
            previous = None
        if previous:
            old = _carry_verdict(conn, aid, req, previous, pmap, inv["coverage"] != "complete", profile_id, version_id, config.versionrag)
            vs.append(agg.V(req["id"], req["statement"], req["obligation"], req["category"],
                            NEEDS if old["status"] == NOT_MET and _overridden(req, overridden, _lineage(conn, req)) else old["status"],
                            old["reason"], _source_label(conn, req), old["question"], old["basis"]))
            _progress(conn, aid, stage, done=done)
            continue
        d = decide(conn, version_id, req, facts, ctx, llm, budget.left, emb, rr, config, profile_id)
        budget.spend(d.cost, d.tokens)
        calls += d.cost > 0
        rule_settled = d.decided_by == "rule" and d.status != UNKNOWN
        if d.escalate and use_agent and d.decided_by != "budget" and not rule_settled:
            b2 = d
            d, tools = run_agent(conn, aid, version_id, req, facts, ctx, llm, budget, b2.reason, b2.escalate, emb, rr,
                                 profile_id=profile_id)
            if d.decided_by == "budget" or d.reason.startswith("agent unavailable"):
                b2.escalate.append(f"agent stopped: {d.reason}")
                b2.reason += " (agent investigation stopped: budget reached)" if d.decided_by == "budget" else ""
                b2.reads += [p for p in d.reads if p not in b2.reads]
                d, b2.recorded_by = b2, "agent_trace"
            else:
                d.escalate = b2.escalate + d.escalate
                d.params = d.params or b2.params
                d.retrieval, d.conditions = b2.retrieval, b2.conditions
                d = _combine(d, [ConditionResult(c["id"], c["label"], c["mandatory"], RuleResult(c["status"], c["reason"]))
                                 for c in b2.conditions if c["decided_by"] == "rule"])
            d.recorded_by, d.documents = "agent_trace", sorted(tools.read_docs)
        budget_hit |= d.decided_by == "budget"
        quality, uncertainty = evidence_quality(d, facts)
        vid = must(conn.execute(
            """INSERT INTO verdict (assessment_id, requirement_id, status, reason, basis, needs_human, provisional,
                                    question, escalate, escalate_reason, decided_by, required, observed, evidence_quality,
                                    uncertainty, retrieval, reference_gaps)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (aid, req["id"], d.status, d.reason, d.basis, d.needs_human, inv["coverage"] != "complete", d.question,
             bool(d.escalate), "; ".join(dict.fromkeys(d.escalate)) or None, d.decided_by, d.required, d.observed, quality, uncertainty,
             Jsonb(d.retrieval), d.gaps)).fetchone())["id"]
        _record_verdict_links(conn, vid, req, version_id, d, facts, profile_id)
        status = d.status
        if d.status == NOT_MET and (o := _overridden(req, overridden, _lineage(conn, req))):
            status = NEEDS
            conn.execute("UPDATE verdict SET override_status = %s, override_reason = %s, override_by = %s WHERE id = %s",
                         (NEEDS, o["reason"], o["user_name"], vid))
        vs.append(agg.V(req["id"], req["statement"], req["obligation"], req["category"], status, d.reason,
                        _source_label(conn, req), d.question, d.basis))
        _progress(conn, aid, stage, done=done)
    _progress(conn, aid, stage, stage="finalising")
    refs = [agg.Ref(r["label"], r["kind"], r["essential"], r["status"] if config.coverage else "read") for r in inv["documents"]]
    product_cpv = [c for f in facts if f.attribute == "product" for c in f.value.get("cpv", [])]
    region: list[str] = next((f.value.get("itl", []) for f in facts if f.attribute == "region"), [])
    rel = agg.relevance(ctx.fields["lot.cpv"] or [], product_cpv, ctx.fields["lot.regions"] or [], region)
    rejected = must(conn.execute(
        """SELECT count(*) n FROM requirement_rejection rj LEFT JOIN passage p ON p.id = rj.passage_id LEFT JOIN document d ON d.id = p.document_id
           WHERE rj.version_id = %s AND rj.obligation = 'mandatory' AND rj.kind <> 'condition'
             AND (d.owner_profile_id IS NULL OR d.owner_profile_id = %s)""", (version_id, profile_id)).fetchone())["n"]
    dims = agg.dimensions(vs, refs, rel, inv["coverage"], rejected)
    # One transaction: the old assessment stops being current exactly when the new one becomes current.
    if not shadow:
        conn.execute("""UPDATE assessment SET superseded = true WHERE lot_id = %s AND profile_id = %s AND status = 'done'
                        AND NOT superseded AND NOT shadow""", (lot_id, profile_id))
    finished = conn.execute(
        """UPDATE assessment SET recommendation = %s, dimensions = %s, provisional = %s, status = 'done', cost_usd = %s,
                  model_calls = %s, budget_reached = %s, finished_at = now(), progress = %s
           WHERE id = %s AND status = 'running' RETURNING id""",
        (dims.recommendation, Jsonb(dims.__dict__), dims.provisional, budget.cost, calls, budget_hit,
         Jsonb({"stage": "done", "done": len(reqs), "total": len(reqs)}), aid)).fetchone()
    if not finished:
        raise AssessmentFailed("this run was closed by a newer attempt before it could finish")
    if finalise:
        finalise(aid)
    conn.commit()
