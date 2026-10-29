import json

from tenderfit.llm import UNTRUSTED_RULE, untrusted

VERDICT_VERSION = "verdict-v1"

VERDICT_SYSTEM = f"""You judge whether a supplier meets one requirement of a UK public tender, using only the passages
and profile facts given. TenderFit recommends; a person decides.

{UNTRUSTED_RULE}

Statuses:
- met: an explicit supporting profile fact satisfies the requirement. List its id in supporting_fact_ids.
- not_met: a profile fact explicitly contradicts the requirement (e.g. 300 desks declared against a mandatory 1,200).
  List it in contrary_fact_ids. Missing evidence is never not_met.
- needs_confirmation: likely satisfiable but something must be confirmed: only a declaration where proof is
  required, a time-sensitive or expiring fact, ambiguity, or passages that conflict.
- unknown: no relevant fact, or the passages do not say enough either way.
Never assume certificates, test reports, experience, prices or commitments that are not in the facts.
Cite passages by id with an exact quote copied from that passage (8 to 50 words); quotes that do not match are
discarded. Set conflict true if passages disagree. Ask one short targeted question when the user could settle it."""

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "reason", "supporting_fact_ids", "contrary_fact_ids", "citations", "question", "conflict"],
    "properties": {
        "status": {"type": "string", "enum": ["met", "not_met", "needs_confirmation", "unknown"]},
        "reason": {"type": "string"},
        "supporting_fact_ids": {"type": "array", "items": {"type": "integer"}},
        "contrary_fact_ids": {"type": "array", "items": {"type": "integer"}},
        "citations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["passage_id", "quote"],
            "properties": {"passage_id": {"type": "integer"}, "quote": {"type": "string"}}}},
        "question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "conflict": {"type": "boolean"},
    },
}


def verdict_user(req: dict, passages: list[dict], facts: list[dict], today: str, deadline: str | None) -> str:
    ps = "\n\n".join(untrusted("tender_passage", p["text"], id=p["id"], document=p["document"],
                               section=p["section"] or "", page=p["page"] or "") for p in passages)
    fs = json.dumps(facts, default=str, indent=1)
    return (f"Today: {today}. Submission deadline: {deadline or 'not stated'}.\n\n"
            f"Requirement ({req['obligation']}, {req['category']}): {req['statement']}\n"
            f"Source quote: \"{req['quote']}\" (passage {req['passage_id']})\n"
            f"Equivalent allowed: {req['equivalent_allowed']}. Evidence the buyer asks for: "
            f"{', '.join(req['evidence_needed']) or 'not stated'}.\n\n"
            f"Supplier profile facts (with evidence status and validity):\n{fs}\n\nPassages:\n\n{ps}")
