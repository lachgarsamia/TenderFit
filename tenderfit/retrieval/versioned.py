"""Resolve explicit evidence paths inside one supplier-visible release."""
from collections import defaultdict
from dataclasses import dataclass, field

from tenderfit.db import Conn
from tenderfit.documents import references


@dataclass
class EvidencePaths:
    links: list[dict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


def resolve(conn: Conn, version: int, source: int, labels: set[str], profile: int | None) -> EvidencePaths:
    result = EvidencePaths()
    frontier: list[tuple[int, list[str], list[int], list[str]]] = [(source, sorted({s.casefold() for s in labels}), [source], [])]
    for depth in range(2):
        following = []
        for origin, wanted, path, reasons in frontier:
            if not wanted:
                continue
            rows = conn.execute("""
                SELECT e.to_passage_id AS id, e.kind, e.label, p.text
                FROM passage_edge e
                JOIN passage p ON p.id = e.to_passage_id JOIN document d ON d.id = p.document_id
                JOIN passage s ON s.id = e.from_passage_id JOIN document sd ON sd.id = s.document_id
                WHERE e.version_id = %(v)s AND d.version_id = %(v)s AND sd.version_id = %(v)s
                  AND d.access = 'read' AND sd.access = 'read'
                  AND (d.owner_profile_id IS NULL OR d.owner_profile_id = %(profile)s)
                  AND (sd.owner_profile_id IS NULL OR sd.owner_profile_id = %(profile)s)
                  AND s.id = %(src)s AND lower(e.label) = ANY(%(labels)s)
                ORDER BY e.label, p.id
                """, dict(v=version, profile=profile, src=origin, labels=wanted)).fetchall()
            groups = defaultdict(list)
            for row in rows:
                groups[row["label"].casefold()].append(row)
            for label in wanted:
                matches = groups[label]
                if len({row["id"] for row in matches}) != 1:
                    result.gaps.append(f"{label} ({'ambiguous' if matches else 'unresolved'})")
                    continue
                row = matches[0]
                if row["id"] in path:
                    continue
                route = path + [row["id"]]
                steps = reasons + [row["label"]]
                result.links.append(dict(id=row["id"], kind=row["kind"], label=row["label"], path=route, steps=steps))
                if depth == 0:
                    nested = {r.label.casefold() for r in references.find(row["text"]) if r.kind != "named"}
                    # Follow recognised defined terms even when the appendix does not spell out the definition location.
                    terms = conn.execute("""SELECT DISTINCT lower(label) term FROM passage_edge WHERE version_id = %s
                        AND from_passage_id = %s AND kind = 'DEFINED_BY' AND to_passage_id IN
                        (SELECT p.id FROM passage p JOIN document d ON d.id=p.document_id WHERE d.version_id=%s
                         AND (d.owner_profile_id IS NULL OR d.owner_profile_id=%s))""", (version, row["id"], version, profile))
                    nested.update(r["term"] for r in terms)
                    following.append((row["id"], sorted(nested), route, steps))
        frontier = following
    result.gaps = sorted(set(result.gaps))
    return result


def provenance(conn: Conn, version: int, profile: int | None, entries: list[dict]) -> list[dict]:
    ids = [entry["passage_id"] for entry in entries]
    rows = conn.execute("""SELECT p.id, d.id AS document_id, d.filename AS document, p.page, p.section,
                            d.version_id, nv.release_id, nv.ocid, nv.source_url AS notice_url,
                            p.sha256 AS passage_hash, predecessor.prior_passage_id, predecessor.prior_version_id
                           FROM passage p JOIN document d ON d.id=p.document_id
                           JOIN notice_version nv ON nv.id=d.version_id
                           LEFT JOIN LATERAL (
                             SELECT old.id AS prior_passage_id, od.version_id AS prior_version_id
                             FROM change_event ev CROSS JOIN LATERAL jsonb_array_elements(ev.passages) edit
                             JOIN passage old ON old.id=(edit->>'old_id')::int
                             JOIN document od ON od.id=old.document_id
                             WHERE ev.to_version_id=d.version_id AND ev.from_version_id=od.version_id
                               AND edit->>'kind'='modified' AND (edit->>'new_id')::int=p.id
                               AND (od.owner_profile_id IS NULL OR od.owner_profile_id=%s)
                             ORDER BY ev.from_version_id DESC LIMIT 1
                           ) predecessor ON true
                           WHERE p.id=ANY(%s) AND d.version_id=%s
                           AND (d.owner_profile_id IS NULL OR d.owner_profile_id=%s)""", (profile, ids, version, profile))
    sources = {row.pop("id"): row for row in rows}
    if set(ids) != set(sources):
        raise ValueError("retrieval evidence outside the selected snapshot")
    return [{**entry, **sources[entry["passage_id"]], "method": "VersionRAG"} for entry in entries]


def reusable(conn: Conn, verdict: int, req: dict, version: int, profile: int, mapping: dict[int, int]) -> bool:
    old = conn.execute("""SELECT v.retrieval, v.reference_gaps, a.version_id FROM verdict v
        JOIN assessment a ON a.id=v.assessment_id WHERE v.id=%s AND a.profile_id=%s""", (verdict, profile)).fetchone()
    if not old or not old["retrieval"] or any(e.get("method") != "VersionRAG" for e in old["retrieval"]):
        return False
    if old["version_id"] == version:
        return True
    # Compare the resolved paths as well as the planner's recorded read-set. A newly added definition
    # can resolve a former gap or make a formerly unique target ambiguous without changing the source.
    used = {e["passage_id"] for e in old["retrieval"]}
    used.update(r["passage_id"] for r in conn.execute("SELECT passage_id FROM verdict_citation WHERE verdict_id=%s", (verdict,)))
    if not used.issubset(mapping) or old["reference_gaps"]:
        return False
    labels = {r.label for r in references.find(req["quote"]) if r.kind != "named"}
    labels.update(req["refs"] or [])
    labels.update(req["defined_terms"] or [])
    paths = resolve(conn, version, req["passage_id"], labels, profile)
    previous = {tuple(mapping.get(pid) for pid in e["path"]) for e in old["retrieval"] if "path" in e}
    return not paths.gaps and previous == {tuple(link["path"]) for link in paths.links}
