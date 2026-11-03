"""Deterministic check: does another passage state the same obligation with a different number?"""
import re

import psycopg

from tenderfit.documents.text import normalise
from tenderfit.extraction.validate import numbers_in, unit_in

SENTENCE = re.compile(r"(?<=[.;])\s+|\n+")
MIN_OVERLAP = 0.7
STOP = {"the", "a", "an", "of", "to", "and", "or", "shall", "must", "will", "be", "is", "are", "all", "any", "in", "on", "for", "by", "with",
        "within", "least", "at", "no", "more", "than", "not", "supplier", "bidder", "bidders", "tenderer", "tenderers"}


def _terms(text: str) -> set[str]:
    return {w.rstrip("s") for w in normalise(text).split() if w not in STOP and not w.isdigit() and not numbers_in(w)}


def find_conflicts(conn: psycopg.Connection, version_id: int, passage_id: int, quote: str, profile_id: int | None) -> list[dict]:
    """Sentences elsewhere in the pack that share most of the quote's wording and its unit, but state another number."""
    numbers, unit, terms = numbers_in(quote), unit_in(quote), _terms(quote)
    if not numbers or len(terms) < 3:
        return []
    out = []
    for p in conn.execute(
            """SELECT p.id, p.text, p.section, coalesce(d.filename, 'document') AS document FROM passage p JOIN document d ON d.id = p.document_id
               WHERE d.version_id = %s AND d.access = 'read' AND p.id <> %s AND (d.owner_profile_id IS NULL OR d.owner_profile_id = %s)""",
            (version_id, passage_id, profile_id)):
        for sentence in SENTENCE.split(p["text"]):
            other = _terms(sentence)
            if not other or len(terms & other) / len(terms | other) < MIN_OVERLAP or unit_in(sentence) != unit:
                continue
            stated = numbers_in(sentence)
            if stated and not stated & numbers:
                out.append({"passage_id": p["id"], "quote": sentence.strip(), "document": p["document"], "section": p["section"]})
    return out
