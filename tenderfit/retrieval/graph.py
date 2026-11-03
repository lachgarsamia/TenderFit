"""Typed links between passages, stored in Postgres: a requirement that cites Appendix C can reach Appendix C.

Edges come only from references and definitions found in the text. There is no embedding-based linking, so an edge can
always be explained by the words that created it."""
import re

import psycopg

from tenderfit.documents import references

DEFINES = re.compile(r"(?:^|[\n|.]\s*)[\"“']?([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)*)[\"”']?\s*(?:\||:|\bmeans?\b|\bshall mean\b|\bis defined as\b)")
TERM = re.compile(r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b")


def _section_matches(section: str, label: str) -> bool:
    s, low = section.lower().strip(), label.lower().strip()
    key = low.removeprefix("section ").strip()
    return s == low or s == key or s.startswith((f"{low} ", f"{low}:", f"{low}.", f"{low}-", f"{key} ", f"{key}.", f"{key}:"))


def build_graph(conn: psycopg.Connection, version_id: int) -> int:
    """Idempotent. Rebuilt whenever passages may have changed; access is enforced when edges are read."""
    rows = conn.execute(
        """SELECT p.id, p.document_id, p.section, p.text FROM passage p JOIN document d ON d.id = p.document_id
           WHERE d.version_id = %s AND d.access = 'read' ORDER BY p.document_id, p.ordinal""", (version_id,)).fetchall()
    definitions: dict[str, list[int]] = {}
    for p in rows:
        if p["section"] and "defin" in p["section"].lower():
            for term in DEFINES.findall(p["text"]):
                definitions.setdefault(term.lower(), []).append(p["id"])
    n = 0
    for p in rows:
        for ref in references.find(p["text"]):
            if ref.kind == "named":
                continue
            own = [t for t in rows if t["section"] and t["id"] != p["id"] and t["document_id"] == p["document_id"]
                   and _section_matches(t["section"], ref.label)]
            targets = own or [t for t in rows if t["section"] and t["id"] != p["id"] and _section_matches(t["section"], ref.label)]
            for t in targets:
                n += _edge(conn, version_id, p["id"], t["id"], "REFERENCES", ref.label)
        for term in {t.lower(): t for t in TERM.findall(p["text"])}.values():
            for t in definitions.get(term.lower(), []):
                if t != p["id"]:
                    n += _edge(conn, version_id, p["id"], t, "DEFINED_BY", term)
    conn.commit()
    return n


def _edge(conn, version_id: int, a: int, b: int, kind: str, label: str) -> int:
    return conn.execute("""INSERT INTO passage_edge (version_id, from_passage_id, to_passage_id, kind, label)
                           VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""", (version_id, a, b, kind, label)).rowcount


def expand(conn: psycopg.Connection, from_passage: int, labels: set[str], profile_id: int | None, limit: int = 6) -> list[dict]:
    """One hop from the requirement's own passage, only along links the requirement itself names."""
    if not labels:
        return []
    return conn.execute(
        """SELECT e.to_passage_id AS id, e.kind, e.label FROM passage_edge e
           JOIN passage p ON p.id = e.to_passage_id JOIN document d ON d.id = p.document_id
           WHERE e.from_passage_id = %s AND lower(e.label) = ANY(%s)
             AND (d.owner_profile_id IS NULL OR d.owner_profile_id = %s)
           ORDER BY e.kind DESC, p.document_id, p.ordinal LIMIT %s""",
        (from_passage, [x.lower() for x in labels], profile_id, limit)).fetchall()


def reference_gaps(conn: psycopg.Connection, version_id: int, passage_id: int, labels: set[str], profile_id: int | None) -> list[str]:
    """References with no passage to follow and no document on record: something this verdict did not get to read."""
    gaps = []
    for label in sorted(labels):
        low = label.lower()
        if conn.execute("""SELECT 1 FROM passage_edge e JOIN passage p ON p.id = e.to_passage_id JOIN document d ON d.id = p.document_id
                           WHERE e.from_passage_id = %s AND lower(e.label) = %s
                             AND (d.owner_profile_id IS NULL OR d.owner_profile_id = %s) LIMIT 1""",
                        (passage_id, low, profile_id)).fetchone():
            continue
        if conn.execute("""SELECT 1 FROM passage p JOIN document d ON d.id = p.document_id
                           WHERE d.version_id = %s AND (d.owner_profile_id IS NULL OR d.owner_profile_id = %s) AND p.id <> %s
                             AND (lower(p.section) = %s OR lower(p.section) LIKE %s) LIMIT 1""",
                        (version_id, profile_id, passage_id, low, low + " %")).fetchone():
            continue
        ref = conn.execute(
            """SELECT coalesce(s.status, r.status) AS status FROM document_ref r
               LEFT JOIN document_ref_state s ON s.ref_id = r.id AND s.profile_id = %s
               WHERE r.version_id = %s AND lower(r.label) = %s AND (r.owner_profile_id IS NULL OR r.owner_profile_id = %s)
               ORDER BY coalesce(s.status, r.status) = 'read' DESC LIMIT 1""", (profile_id, version_id, low, profile_id)).fetchone()
        if ref and ref["status"] in ("read", "confirmed_none"):
            continue
        gaps.append(label)
    return gaps
