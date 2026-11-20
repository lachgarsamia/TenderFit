"""A frozen copy of everything one assessment was decided on: the supplier's facts, the requirements and their conditions,
and the tender context. The supplier profile has no versions of its own, so this snapshot is the profile version a
simulation uses; its hash makes that exact."""
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime

from tenderfit.assessment.pipeline import LONDON, load_facts, lot_context
from tenderfit.db import Conn
from tenderfit.rules.engine import Fact
from tenderfit.simulation.changes import SimulationConflict, SimulationError, SimulationNotFound

ENGINE_VERSION = "sim-v2"  # bump when what a simulation computes, or how, changes meaning
DATE_FIELDS = ("notice.deadline", "lot.contract_end")
FACT_DATES = ("valid_until", "confirm_by", "confirmed_at")


@dataclass
class Snapshot:
    assessment_id: int
    profile_id: int
    lot_id: int
    version_id: int
    as_of: date
    facts: list[Fact]
    requirements: list[dict]  # id, statement, quote, category, obligation, ..., baseline verdict, conditions
    fields: dict              # Context.fields of the baseline's notice version

    # None preserves the exact sim-v1 JSON and hash; new snapshots freeze an entry for every fact.
    support: dict[int, list[dict]] | None = None

    def to_json(self) -> dict:
        return {"assessment_id": self.assessment_id, "profile_id": self.profile_id, "lot_id": self.lot_id,
                "version_id": self.version_id, "as_of": self.as_of.isoformat(),
                **({"schema_version": 2} if self.support is not None else {}),
                "facts": [{**_fact_json(f), **({"support": self.support.get(f.id, [])} if self.support is not None else {})} for f in self.facts], "requirements": self.requirements,
                "fields": {k: (v.isoformat() if isinstance(v, date) else v) for k, v in self.fields.items()}}

    @classmethod
    def from_json(cls, d: dict) -> "Snapshot":
        return cls(d["assessment_id"], d["profile_id"], d["lot_id"], d["version_id"], date.fromisoformat(d["as_of"]),
                   [_fact_from(f) for f in d["facts"]], d["requirements"],
                   {k: (date.fromisoformat(v) if k in DATE_FIELDS and v else v) for k, v in d["fields"].items()},
                   {f["id"]: f.get("support", []) for f in d["facts"]} if d.get("schema_version") == 2 else None)

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fact_json(f: Fact) -> dict:
    d = asdict(f)
    for k in FACT_DATES:
        d[k] = d[k].isoformat() if d[k] else None
    return d


def _fact_from(d: dict) -> Fact:
    return Fact(**{**{k: v for k, v in d.items() if k != "support"}, **{k: date.fromisoformat(d[k]) if d[k] else None for k in FACT_DATES}})


def _conditions(conn: Conn, req: dict) -> list[dict]:
    """The same rows pipeline._rule_outcomes evaluates, including its fallback for requirements without condition rows."""
    rows = conn.execute("""SELECT id, ordinal, label, condition, quote, mandatory FROM requirement_condition
                           WHERE requirement_id = %s ORDER BY ordinal""", (req["id"],)).fetchall()
    return [dict(r) for r in rows] or [{"id": None, "ordinal": 0, "label": req["statement"], "condition": req["condition"],
                                        "quote": req["quote"], "mandatory": True}]


def baseline(conn: Conn, profile_id: int, assessment_id: int) -> dict:
    a = conn.execute("SELECT * FROM assessment WHERE id = %s AND profile_id = %s", (assessment_id, profile_id)).fetchone()
    if not a:
        raise SimulationNotFound("assessment not found")
    if a["shadow"]:
        raise SimulationConflict("a shadow run is an audit of another assessment, not a baseline")
    if a["status"] != "done":
        raise SimulationConflict(f"only a completed assessment can be a baseline; this one is {a['status']}")
    return a


def take_snapshot(conn: Conn, profile_id: int, assessment_id: int, as_of: date | None = None) -> tuple[Snapshot, dict]:
    """Reads only. Returns the snapshot and the drift: what changed in the live profile after the baseline ran."""
    a = baseline(conn, profile_id, assessment_id)
    as_of = as_of or datetime.now(LONDON).date()
    try:
        _, ctx = lot_context(conn, a["lot_id"], as_of, a["version_id"])
    except ValueError as e:
        raise SimulationError(f"the baseline's notice version cannot be read: {e}") from e
    reqs = []
    for r in conn.execute(
            """SELECT r.*, v.id AS verdict_id, v.status AS verdict_status FROM verdict v JOIN requirement r ON r.id = v.requirement_id
               WHERE v.assessment_id = %s ORDER BY (r.obligation = 'mandatory') DESC, r.id""", (assessment_id,)).fetchall():
        reqs.append({"id": r["id"], "statement": r["statement"], "quote": r["quote"], "category": r["category"],
                     "obligation": r["obligation"], "evidence_needed": list(r["evidence_needed"]),
                     "equivalent_allowed": r["equivalent_allowed"], "passage_id": r["passage_id"],
                     "baseline_verdict_id": r["verdict_id"], "baseline_status": r["verdict_status"],
                     "conditions": _conditions(conn, r)})
    snap = Snapshot(assessment_id, profile_id, a["lot_id"], a["version_id"], as_of,
                    load_facts(conn, profile_id, a["lot_id"], a["version_id"]), reqs, dict(ctx.fields))
    snap.support = {f.id: [] for f in snap.facts}
    for row in conn.execute(
            """SELECT s.fact_id, s.evidence_file_id, s.page, s.quote, s.verified,
                      (s.human_verified_by IS NOT NULL) AS human_verified,
                      (SELECT v.level FROM fact_verification v WHERE v.fact_id = s.fact_id
                       AND v.evidence_file_id = s.evidence_file_id ORDER BY v.id DESC LIMIT 1) AS verification_level
               FROM fact_support s JOIN profile_fact f ON f.id = s.fact_id
               JOIN evidence_file e ON e.id = s.evidence_file_id
               WHERE f.profile_id = %s AND e.profile_id = %s ORDER BY s.id""", (profile_id, profile_id)):
        if row["fact_id"] in snap.support:
            snap.support[row["fact_id"]].append({k: v for k, v in row.items() if k != "fact_id"})
    return snap, drift(conn, a, as_of)


def drift(conn: Conn, a: dict, as_of: date) -> dict:
    """Why control (rules on today's snapshot) may differ from the baseline for reasons that have nothing to do with a hypothesis."""
    ran = a["finished_at"] or a["created_at"]
    changed = [r["id"] for r in conn.execute(
        "SELECT id FROM profile_fact WHERE profile_id = %s AND updated_at > %s ORDER BY id", (a["profile_id"], ran))]
    confirmed = [r["fact_id"] for r in conn.execute(
        """SELECT DISTINCT fact_id FROM lot_fact_confirmation WHERE lot_id = %s AND profile_id = %s AND created_at > %s
           ORDER BY fact_id""", (a["lot_id"], a["profile_id"], ran))]
    current = conn.execute("""SELECT o.current_version_id FROM lot l JOIN opportunity o ON o.id = l.opportunity_id
                              WHERE l.id = %s""", (a["lot_id"],)).fetchone()
    return {"facts_changed_since_baseline": changed, "confirmations_since_baseline": confirmed,
            "as_of_differs_from_baseline": as_of != ran.astimezone(LONDON).date(),
            "baseline_superseded": a["superseded"],
            "baseline_on_current_version": bool(current and current["current_version_id"] == a["version_id"])}
