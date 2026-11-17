"""What a run reads live, outside the snapshot, and how to tell whether it changed. Reads only.

The snapshot freezes the supplier's facts, the requirements' wording and conditions, the tender fields and the date. Its
hash covers exactly that, and nothing more. A run also reads these inputs from live tables, so they can change between
the moment a simulation is created and the moment it runs:

  overrides         confirmation        the supplier's recorded overrides of a not-met verdict (point G6)
  answers           question            answers the supplier gave to questions about a requirement
  requirements      requirement         the live requirement rows (references, lineage, source location)
  documents         document_ref, document_ref_state, document, passage
                                        the tender pack as this supplier sees it: what was read, uploaded or set aside,
                                        and the passages a contradiction check scans
  rejections        requirement_rejection
                                        mandatory requirements found but not extracted, which lower coverage
  baseline_record   assessment, verdict, dependency
                                        the baseline itself; it should never change, and is checked rather than assumed

Control and simulated are evaluated in one REPEATABLE READ, READ ONLY transaction (isolation.read_only), so both see
exactly the same values of all of these, and so does the provenance recorded with the result. read() digests them at
creation (stored in the simulation's drift, which cannot be updated) and again at run time. compare() turns any
difference into input drift, reported even when no requirement's own status moves."""
import hashlib
import json

from tenderfit.assessment.pipeline import _lineage
from tenderfit.changes.impact import load_deps
from tenderfit.db import Conn
from tenderfit.documents import service as docs
from tenderfit.simulation.snapshot import Snapshot

TABLES: dict[str, tuple[str, ...]] = {
    "overrides": ("confirmation",),
    "answers": ("question",),
    "requirements": ("requirement",),
    "documents": ("document_ref", "document_ref_state", "document", "passage"),
    "rejections": ("requirement_rejection",),
    "baseline_record": ("assessment", "verdict", "dependency"),
}
PER_REQUIREMENT = ("overrides", "answers", "requirements")
DETAIL = {
    "overrides": "an override of this requirement was recorded or changed",
    "answers": "an answer about this requirement was recorded or changed",
    "requirements": "the live requirement record changed",
    "documents": "the tender documents changed (read, uploaded, set aside or re-processed)",
    "rejections": "the requirements found but not extracted changed",
    "baseline_record": "the baseline assessment record changed",
}
FROZEN = ("supplier facts", "requirement wording and conditions", "tender fields", "as_of date")


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def read(conn: Conn, snap: Snapshot) -> dict:
    """{input: {"sha256": digest, "requirements": {requirement id: digest}?}}, in the caller's transaction."""
    rids = [r["id"] for r in snap.requirements]
    live = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM requirement WHERE id = ANY(%s) ORDER BY id", (rids,))}
    lineage = {rid: _lineage(conn, live[rid]) if rid in live else [rid] for rid in rids}
    every = sorted({i for ids in lineage.values() for i in ids})
    overrides = conn.execute(
        """SELECT id, (payload->>'requirement_id')::bigint AS requirement_id, reason FROM confirmation
           WHERE point = 'G6' AND decision = 'override' AND profile_id = %s AND (payload->>'requirement_id')::bigint = ANY(%s)
           ORDER BY id""", (snap.profile_id, every)).fetchall()
    answers = conn.execute(
        """SELECT id, requirement_id, answer, answered_at FROM question
           WHERE requirement_id = ANY(%s) AND answer IS NOT NULL AND profile_id IS NOT DISTINCT FROM %s ORDER BY id""",
        (every, snap.profile_id)).fetchall()
    per_req: dict[str, dict[int, object]] = {
        "overrides": {rid: [o for o in overrides if o["requirement_id"] in lineage[rid]] for rid in rids},
        "answers": {rid: [a for a in answers if a["requirement_id"] in lineage[rid]] for rid in rids},
        "requirements": {rid: live.get(rid) for rid in rids},
    }
    out: dict[str, dict] = {}
    for name, values in per_req.items():
        digests = {str(rid): _sha(v) for rid, v in values.items()}
        out[name] = {"sha256": _sha(digests), "requirements": digests}
    inventory = {k: v for k, v in docs.inventory(conn, snap.version_id, snap.profile_id).items() if k != "checked_at"}
    readable = conn.execute(
        """SELECT d.id, d.access, d.owner_profile_id, (SELECT count(*) FROM passage p WHERE p.document_id = d.id) AS passages
           FROM document d WHERE d.version_id = %s AND (d.owner_profile_id IS NULL OR d.owner_profile_id = %s) ORDER BY d.id""",
        (snap.version_id, snap.profile_id)).fetchall()
    out["documents"] = {"sha256": _sha([inventory, readable])}
    out["rejections"] = {"sha256": _sha(conn.execute(
        """SELECT rj.* FROM requirement_rejection rj LEFT JOIN passage p ON p.id = rj.passage_id
           LEFT JOIN document d ON d.id = p.document_id
           WHERE rj.version_id = %s AND (d.owner_profile_id IS NULL OR d.owner_profile_id = %s) ORDER BY rj.id""",
        (snap.version_id, snap.profile_id)).fetchall())}
    baseline = conn.execute("SELECT * FROM assessment WHERE id = %s", (snap.assessment_id,)).fetchone()
    verdicts = conn.execute("SELECT * FROM verdict WHERE assessment_id = %s ORDER BY id", (snap.assessment_id,)).fetchall()
    out["baseline_record"] = {"sha256": _sha([baseline, verdicts, sorted(map(repr, load_deps(conn, snap.assessment_id)))])}
    return out


def compare(at_creation: dict | None, at_run: dict) -> list[dict]:
    """Input drift between creation and run: [{input, requirement_id (None for the whole lot), detail}]."""
    if not at_creation:
        return [{"input": "all", "requirement_id": None,
                 "detail": "this simulation was created before live inputs were recorded, so changes since then cannot be detected"}]
    out = []
    for name, now in at_run.items():
        then = at_creation.get(name) or {}
        if then.get("sha256") == now["sha256"]:
            continue
        if name in PER_REQUIREMENT and "requirements" in then:
            changed = sorted(int(rid) for rid, d in now["requirements"].items() if then["requirements"].get(rid) != d)
            out += [{"input": name, "requirement_id": rid, "detail": DETAIL[name]} for rid in changed]
        else:
            out.append({"input": name, "requirement_id": None, "detail": DETAIL[name]})
    return out


def since_baseline(conn: Conn, snap: Snapshot, baseline_finished) -> list[dict]:
    """Overrides and answers recorded after the baseline ran: they move control away from the baseline, often without any
    requirement's status changing, because an override only changes how a not-met verdict is aggregated."""
    rids = [r["id"] for r in snap.requirements]
    live = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM requirement WHERE id = ANY(%s)", (rids,))}
    out = []
    for rid in rids:
        ids = _lineage(conn, live[rid]) if rid in live else [rid]
        if conn.execute("""SELECT 1 FROM confirmation WHERE point = 'G6' AND decision = 'override' AND profile_id = %s
                             AND (payload->>'requirement_id')::bigint = ANY(%s) AND created_at > %s LIMIT 1""",
                        (snap.profile_id, ids, baseline_finished)).fetchone():
            out.append({"input": "overrides", "requirement_id": rid, "detail": "an override was recorded after the baseline ran"})
        if conn.execute("""SELECT 1 FROM question WHERE requirement_id = ANY(%s) AND answer IS NOT NULL
                             AND profile_id IS NOT DISTINCT FROM %s AND answered_at > %s LIMIT 1""",
                        (ids, snap.profile_id, baseline_finished)).fetchone():
            out.append({"input": "answers", "requirement_id": rid, "detail": "an answer was recorded after the baseline ran"})
    return out
