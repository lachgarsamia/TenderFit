from typing import Literal

from pydantic import BaseModel

from tenderfit.rules.engine import Condition

CATEGORIES = ["quantity", "delivery", "experience", "standard", "certificate", "insurance", "financial", "other"]


class Requirement(BaseModel):
    passage_id: int
    lot: str | None
    statement: str
    category: Literal["quantity", "delivery", "experience", "standard", "certificate", "insurance", "financial", "other"]
    obligation: Literal["mandatory", "scored", "informational"]
    condition: Condition | None
    equivalent_allowed: bool
    evidence_needed: list[str]
    quote: str
    section: str | None
    page: int | None
    references: list[str] = []
    defined_terms: list[str] = []


def _nullable(t):
    return {"anyOf": [t, {"type": "null"}]}


CONDITION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "fact", "bound", "years", "anchor", "min_units", "match"],
    "properties": {
        "kind": {"type": "string", "enum": ["at_least", "at_most", "within_years", "region_covered", "has_evidence",
                                            "min_amount"]},
        "fact": {"type": "string", "enum": ["capacity.max_units", "capacity.lead_days", "experience.end", "region",
                                            "test_report", "certificate", "insurance.amount_gbp", "service", "product",
                                            "registration"]},
        "bound": _nullable({"anyOf": [{"type": "number"}, {"type": "string"}]}),
        "years": _nullable({"type": "integer"}),
        "anchor": _nullable({"type": "string"}),
        "min_units": _nullable({"anyOf": [{"type": "number"}, {"type": "string"}]}),
        "match": _nullable({"type": "string"}),
    },
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requirements"],
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["passage_id", "lot", "statement", "category", "obligation", "condition",
                             "equivalent_allowed", "evidence_needed", "quote", "section", "page", "references",
                             "defined_terms"],
                "properties": {
                    "passage_id": {"type": "integer"},
                    "lot": _nullable({"type": "string"}),
                    "statement": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "obligation": {"type": "string", "enum": ["mandatory", "scored", "informational"]},
                    "condition": _nullable(CONDITION_SCHEMA),
                    "equivalent_allowed": {"type": "boolean"},
                    "evidence_needed": {"type": "array", "items": {"type": "string"}},
                    "quote": {"type": "string"},
                    "section": _nullable({"type": "string"}),
                    "page": _nullable({"type": "integer"}),
                    "references": {"type": "array", "items": {"type": "string"}},
                    "defined_terms": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}
