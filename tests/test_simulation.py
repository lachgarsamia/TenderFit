"""Isolated simulations: a hypothesis about an assessment never changes the assessment, or anything else that is live.

The oracle for every isolation test is `live_fingerprint`: a hash of every row of every non-simulation table, plus the
value of every non-simulation sequence. A changed sequence means a write to a live table was attempted, even if it was
later rolled back, so equal fingerprints mean nothing live was written, or even tried."""
import re
from datetime import date
from pathlib import Path

import psycopg
import pytest

from tenderfit.api.assessments import get_assessment
from tenderfit.assessment.pipeline import assess_lot, load_facts
from tenderfit.db import MIGRATIONS
from tenderfit.rules.engine import Fact
from tenderfit.simulation import snapshot as snapshots
from tenderfit.simulation import store
from tenderfit.simulation.changes import KINDS, SimulationError, SimulationNotFound, dump_change, parse_change
from tenderfit.simulation.scenario import apply_changes
from tenderfit.simulation.snapshot import Snapshot
from tests.test_isolation import KW, world  # noqa: F401  (fixture)
from tests.test_pipeline import TODAY

ROOT = Path(__file__).resolve().parent.parent


def live_fingerprint(db) -> dict[str, str]:
    tables = [r["tablename"] for r in db.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename NOT LIKE 'simulation%' ORDER BY 1")]
    out = {t: db.execute(f"SELECT md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) h FROM {t} t").fetchone()["h"]
           for t in tables}
    for r in db.execute("""SELECT sequencename, last_value FROM pg_sequences
                           WHERE schemaname = 'public' AND sequencename NOT LIKE 'simulation%'"""):
        out[f"seq:{r['sequencename']}"] = str(r["last_value"])
    db.rollback()
    return out


@pytest.fixture
def base(world):  # noqa: F811
    """Atlas and Pennine each assessed once on the Northfield lot. Returns ids and an idle connection."""
    db, atlas, pennine, lot, vid, llm = world
    a = assess_lot(db, lot, atlas, llm, **KW)
    p = assess_lot(db, lot, pennine, llm, **KW)
    reqs = {r["statement"].split(" ")[0]: r["id"] for r in db.execute(
        "SELECT r.id, r.statement FROM verdict v JOIN requirement r ON r.id = v.requirement_id WHERE v.assessment_id = %s", (a,))}
    facts = {r["attribute"]: r["id"] for r in db.execute(
        "SELECT DISTINCT ON (attribute) id, attribute FROM profile_fact WHERE profile_id = %s ORDER BY attribute, id", (atlas,))}
    other_fact = db.execute("SELECT id FROM profile_fact WHERE profile_id = %s LIMIT 1", (pennine,)).fetchone()["id"]
    db.rollback()
    return {"db": db, "atlas": atlas, "pennine": pennine, "lot": lot, "vid": vid, "llm": llm, "a": a, "p": p,
            "lead": reqs["Deliver"], "quantity": reqs["Supply"], "install": reqs["Installation"], "standard": reqs["Desks"],
            "facts": facts, "other_fact": other_fact}


def valid_changes(b) -> list[dict]:
    """One of every kind, each valid against the Atlas baseline."""
    f = b["facts"]
    return [
        {"kind": "set_condition_bound", "requirement_id": b["lead"], "condition": 0, "bound": 45},
        {"kind": "set_condition_match", "requirement_id": b["standard"], "condition": 0, "match": "BS EN 1729-1"},
        {"kind": "set_fact_validity", "fact_id": f["insurance"], "valid_until": "2026-11-20"},
        {"kind": "set_fact_evidence", "fact_id": f["certificate"], "evidence_status": "document_on_file"},
        {"kind": "set_fact_value", "fact_id": f["insurance"], "key": "amount_gbp", "value": 10_000_000},
        {"kind": "add_fact", "attribute": "certificate", "label": "ISO 14001 certificate", "value": {"standard": "ISO 14001"},
         "evidence_status": "document_on_file"},
        {"kind": "set_fact_evidence", "fact_id": -1, "evidence_status": "declared"},
        {"kind": "set_notice_field", "field": "lot.quantity", "value": 400},
        {"kind": "set_notice_field", "field": "notice.deadline", "value": "2026-12-15"},
        {"kind": "set_requirement_text", "requirement_id": b["install"], "statement": "Installation and removal of old desks included"},
        {"kind": "remove_fact", "fact_id": f["service"]},
    ]


# --- 1. Creating a simulation does not modify the baseline ---------------------------------------------------------------

def test_creating_a_simulation_changes_nothing_live(base):
    db = base["db"]
    before = live_fingerprint(db)
    sid = store.create(db, base["atlas"], base["a"], "tester@test.example", "lead time question", as_of=TODAY)
    assert live_fingerprint(db) == before
    row = store.get(db, base["atlas"], sid)
    assert row["status"] == "created" and row["changes"] == []
    db.rollback()


# --- 2. Applying hypothetical changes does not modify live verdicts ------------------------------------------------------

def test_every_kind_of_change_is_recorded_and_live_state_is_untouched(base):
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    before = live_fingerprint(db)
    changes = valid_changes(base)
    assert {c["kind"] for c in changes} == set(KINDS), "this test covers every kind of change"
    for n, c in enumerate(changes, start=1):
        assert store.add_change(db, base["atlas"], sid, c) == n
    assert live_fingerprint(db) == before

    s = store.scenario(db, base["atlas"], sid)
    db.rollback()
    by_id = {f.id: f for f in s.facts}
    assert by_id[base["facts"]["insurance"]].value["amount_gbp"] == 10_000_000
    assert by_id[base["facts"]["insurance"]].valid_until == date(2026, 11, 20)
    assert by_id[-1].label == "ISO 14001 certificate" and by_id[-1].evidence_status == "declared"
    assert base["facts"]["service"] not in by_id
    assert s.fields["lot.quantity"] == 400.0 and s.fields["notice.deadline"] == date(2026, 12, 15)
    assert s.requirements[base["lead"]]["conditions"][0]["condition"]["bound"] == 45
    assert ("requirement", str(base["lead"])) in s.touched and ("field", "lot.quantity") in s.touched


# --- 3. Simulated state stays separate from live assessment state --------------------------------------------------------

def test_simulation_state_lives_only_in_simulation_tables(base):
    db = base["db"]
    live_view = get_assessment(base["lot"], base["atlas"])
    counts = {t: db.execute(f"SELECT count(*) n FROM {t}").fetchone()["n"] for t in ("assessment", "verdict", "dependency", "question")}
    db.rollback()
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    for c in valid_changes(base):
        store.add_change(db, base["atlas"], sid, c)
    assert get_assessment(base["lot"], base["atlas"]) == live_view, "what the supplier sees is unchanged"
    assert {t: db.execute(f"SELECT count(*) n FROM {t}").fetchone()["n"] for t in counts} == counts
    assert db.execute("SELECT count(*) n FROM simulation_change WHERE simulation_id = %s", (sid,)).fetchone()["n"] == len(valid_changes(base))
    # Results can only be written by a run. Stage 4 has no runner, so nothing can put a result next to the live verdicts yet.
    with pytest.raises(psycopg.errors.RaiseException, match="only be added while the simulation is running"):
        db.execute("INSERT INTO simulation_result (simulation_id, requirement_id, how) VALUES (%s, %s, 'rule')", (sid, base["lead"]))
    db.rollback()


def test_mode_a_code_never_imports_the_simulation_package():
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "tenderfit").rglob("*.py")
                 if "simulation" not in p.parts and re.search(r"tenderfit\.simulation|from tenderfit import simulation", p.read_text())]
    assert offenders == []


# --- 4. A simulation references the right baseline, version and profile state --------------------------------------------

def test_simulation_records_its_exact_baseline_and_profile_snapshot(base):
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    row = store.get(db, base["atlas"], sid)
    a = db.execute("SELECT * FROM assessment WHERE id = %s", (base["a"],)).fetchone()
    assert (row["baseline_assessment_id"], row["profile_id"], row["lot_id"], row["version_id"]) == (a["id"], a["profile_id"], a["lot_id"], a["version_id"])
    snap = Snapshot.from_json(row["snapshot"])
    assert snap.facts == load_facts(db, base["atlas"], base["lot"], base["vid"]), "the snapshot is the profile state used"
    assert row["snapshot_sha256"] == snap.sha256()
    assert row["as_of"] == TODAY and row["engine_version"] == snapshots.ENGINE_VERSION
    statuses = {r["requirement_id"]: r["status"] for r in db.execute("SELECT requirement_id, status FROM verdict WHERE assessment_id = %s", (base["a"],))}
    assert {r["id"]: r["baseline_status"] for r in snap.requirements} == statuses
    assert row["drift"]["facts_changed_since_baseline"] == [] and row["drift"]["baseline_on_current_version"]
    db.rollback()


def test_the_database_rejects_a_baseline_from_another_supplier(base):
    db = base["db"]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("""INSERT INTO simulation (profile_id, lot_id, baseline_assessment_id, version_id, as_of, snapshot, snapshot_sha256,
                                              engine_version, created_by) VALUES (%s, %s, %s, %s, %s, '{}', 'x', 'v', 't')""",
                   (base["atlas"], base["lot"], base["p"], base["vid"], TODAY))
    db.rollback()


def test_only_completed_live_assessments_of_this_supplier_are_baselines(base):
    db = base["db"]
    with pytest.raises(SimulationNotFound):
        store.create(db, base["atlas"], base["p"], "tester")  # Pennine's assessment, asked for by Atlas
    shadow = assess_lot(db, base["lot"], base["atlas"], base["llm"], shadow=True, **KW)
    with pytest.raises(SimulationError, match="shadow"):
        store.create(db, base["atlas"], shadow, "tester")
    failed = db.execute("""INSERT INTO assessment (profile_id, lot_id, version_id, status, superseded) VALUES (%s, %s, %s, 'failed', true)
                           RETURNING id""", (base["atlas"], base["lot"], base["vid"])).fetchone()["id"]
    db.commit()
    with pytest.raises(SimulationError, match="only a completed assessment"):
        store.create(db, base["atlas"], failed, "tester")
    assert db.execute("SELECT count(*) n FROM simulation").fetchone()["n"] == 0
    db.rollback()


def test_a_superseded_historical_baseline_is_kept_and_its_drift_reported(base):
    db = base["db"]
    fact = base["facts"]["capacity"]
    db.execute("UPDATE profile_fact SET updated_at = now() WHERE id = %s", (fact,))
    db.commit()
    assess_lot(db, base["lot"], base["atlas"], base["llm"], **KW)  # supersedes the original baseline
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    drift = store.get(db, base["atlas"], sid)["drift"]
    assert drift["baseline_superseded"] is True
    assert drift["facts_changed_since_baseline"] == [fact]
    db.rollback()


# --- 5. Invalid hypothetical changes are rejected ------------------------------------------------------------------------

def test_invalid_changes_are_rejected_and_nothing_is_recorded(base):
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    f = base["facts"]
    bad = {
        "another supplier's fact": {"kind": "remove_fact", "fact_id": base["other_fact"]},
        "unknown fact": {"kind": "set_fact_validity", "fact_id": 999_999, "valid_until": None},
        "requirement not in the baseline": {"kind": "set_condition_bound", "requirement_id": 999_999, "condition": 0, "bound": 1},
        "no such condition": {"kind": "set_condition_bound", "requirement_id": base["lead"], "condition": 7, "bound": 1},
        "condition read by the model": {"kind": "set_condition_bound", "requirement_id": base["install"], "condition": 0, "bound": 1},
        "bound on an evidence check": {"kind": "set_condition_bound", "requirement_id": base["standard"], "condition": 0, "bound": 1},
        "match on a numeric check": {"kind": "set_condition_match", "requirement_id": base["lead"], "condition": 0, "match": "ISO"},
        "negative bound": {"kind": "set_condition_bound", "requirement_id": base["lead"], "condition": 0, "bound": -1},
        "boolean bound": {"kind": "set_condition_bound", "requirement_id": base["lead"], "condition": 0, "bound": True},
        "field a fact does not have": {"kind": "set_fact_value", "fact_id": f["insurance"], "key": "colour", "value": "red"},
        "wrong type for a fact field": {"kind": "set_fact_value", "fact_id": f["insurance"], "key": "amount_gbp", "value": "lots"},
        "unknown insurance type": {"kind": "set_fact_value", "fact_id": f["insurance"], "key": "type", "value": "pet insurance"},
        "unknown kind of fact": {"kind": "add_fact", "attribute": "horoscope", "label": "x", "value": {}},
        "fact missing its key field": {"kind": "add_fact", "attribute": "certificate", "label": "x", "value": {}},
        "unsupported notice field": {"kind": "set_notice_field", "field": "notice.title", "value": "x"},
        "evidence status outside the schema": {"kind": "set_fact_evidence", "fact_id": f["insurance"], "evidence_status": "maybe"},
        "not a known change": {"kind": "run_sql", "sql": "DELETE FROM verdict"},
    }
    before = live_fingerprint(db)
    for why, change in bad.items():
        with pytest.raises(SimulationError):
            store.add_change(db, base["atlas"], sid, change)
            pytest.fail(f"accepted: {why}")
    assert store.get(db, base["atlas"], sid)["changes"] == []
    db.rollback()
    assert live_fingerprint(db) == before


def test_changes_are_checked_in_order_against_what_came_before(base):
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    store.add_change(db, base["atlas"], sid, {"kind": "remove_fact", "fact_id": base["facts"]["insurance"]})
    with pytest.raises(SimulationError, match="not in this supplier's profile snapshot"):
        store.add_change(db, base["atlas"], sid, {"kind": "set_fact_value", "fact_id": base["facts"]["insurance"],
                                                  "key": "amount_gbp", "value": 1})
    assert len(store.get(db, base["atlas"], sid)["changes"]) == 1
    db.rollback()


def test_the_database_refuses_change_kinds_it_does_not_know(base):
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO simulation_change (simulation_id, ordinal, kind, payload) VALUES (%s, 1, 'run_sql', '{}')", (sid,))
    db.rollback()


def test_code_and_schema_agree_on_the_kinds_of_change():
    sql = (MIGRATIONS / "019_simulations.sql").read_text()
    listed = re.search(r"kind text NOT NULL CHECK \(kind IN \((.*?)\)\)", sql, re.S)
    assert listed and set(re.findall(r"'(\w+)'", listed.group(1))) == set(KINDS)


# --- 6. Discarding a simulation does not alter live data -----------------------------------------------------------------

def test_discarding_is_final_and_changes_nothing_live(base):
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    store.add_change(db, base["atlas"], sid, valid_changes(base)[0])
    before = live_fingerprint(db)
    store.discard(db, base["atlas"], sid)
    assert live_fingerprint(db) == before
    row = store.get(db, base["atlas"], sid)
    assert row["status"] == "discarded" and row["discarded_at"] and len(row["changes"]) == 1, "discarded, not deleted"
    db.rollback()
    with pytest.raises(SimulationError, match="before a simulation runs"):
        store.add_change(db, base["atlas"], sid, valid_changes(base)[1])
    with pytest.raises(SimulationError, match="already discarded"):
        store.discard(db, base["atlas"], sid)
    # The database holds the same line if application code does not.
    with pytest.raises(psycopg.errors.RaiseException, match="discarded"):
        db.execute("UPDATE simulation SET status = 'running' WHERE id = %s", (sid,))
    db.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="only be added while the simulation is created"):
        db.execute("INSERT INTO simulation_change (simulation_id, ordinal, kind, payload) VALUES (%s, 9, 'remove_fact', '{}')", (sid,))
    db.rollback()
    assert live_fingerprint(db) == before


def test_the_lifecycle_and_the_basis_of_a_simulation_are_enforced_by_the_database(base):
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    with pytest.raises(psycopg.errors.RaiseException, match="cannot go from created to completed"):
        db.execute("UPDATE simulation SET status = 'completed' WHERE id = %s", (sid,))
    db.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="cannot change"):
        db.execute("""UPDATE simulation SET snapshot = jsonb_set(snapshot, '{as_of}', '"2001-01-01"') WHERE id = %s""", (sid,))
    db.rollback()
    store.add_change(db, base["atlas"], sid, valid_changes(base)[0])
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        db.execute("UPDATE simulation_change SET payload = '{}' WHERE simulation_id = %s", (sid,))
    db.rollback()


# --- 7. One simulation cannot modify another -----------------------------------------------------------------------------

def test_one_simulation_cannot_reach_another(base):
    db = base["db"]
    a = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    b = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    store.add_change(db, base["atlas"], b, valid_changes(base)[2])

    def rows(sid):
        out = (db.execute("SELECT s::text FROM simulation s WHERE id = %s", (sid,)).fetchone()["s"],
               [r["c"] for r in db.execute("SELECT c::text FROM simulation_change c WHERE simulation_id = %s ORDER BY ordinal", (sid,))])
        db.rollback()
        return out

    b_before = rows(b)
    for c in valid_changes(base):
        store.add_change(db, base["atlas"], a, c)
    store.discard(db, base["atlas"], a)
    assert rows(b) == b_before
    assert store.scenario(db, base["atlas"], b).changes == [parse_change(valid_changes(base)[2])]
    db.rollback()


def test_another_supplier_cannot_read_change_or_discard_a_simulation(base):
    db = base["db"]
    sid = store.create(db, base["atlas"], base["a"], "tester", as_of=TODAY)
    for attempt in (lambda: store.get(db, base["pennine"], sid), lambda: store.discard(db, base["pennine"], sid),
                    lambda: store.add_change(db, base["pennine"], sid, valid_changes(base)[0])):
        with pytest.raises(SimulationNotFound):
            attempt()
        db.rollback()
    assert store.list_for_profile(db, base["pennine"]) == []
    assert [s["id"] for s in store.list_for_profile(db, base["atlas"])] == [sid]
    db.rollback()


# --- 8. Failures leave no partial state ----------------------------------------------------------------------------------

def test_a_failed_write_leaves_no_simulation_and_nothing_live(base, monkeypatch):
    db = base["db"]
    before = live_fingerprint(db)
    real = snapshots.take_snapshot

    def wrong_lot(*args, **kwargs):  # a snapshot that does not match its baseline: the composite key refuses it
        snap, drift = real(*args, **kwargs)
        snap.lot_id = 999_999
        return snap, drift

    monkeypatch.setattr(store, "take_snapshot", wrong_lot)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.create(db, base["atlas"], base["a"], "tester")
    assert db.info.transaction_status == psycopg.pq.TransactionStatus.IDLE, "the failed transaction was closed"
    assert db.execute("SELECT count(*) n FROM simulation").fetchone()["n"] == 0
    db.rollback()
    assert live_fingerprint(db) == before


def test_a_write_attempted_while_reading_the_baseline_is_refused_by_postgres(base, monkeypatch):
    """Stand-in for reused assessment code that writes as a side effect (verdict_cache, llm_call): it cannot, in a read phase."""
    db = base["db"]
    before = live_fingerprint(db)
    real = snapshots.load_facts

    def writes_on_the_side(conn, *args):
        conn.execute("INSERT INTO verdict_cache (key, result) VALUES ('side effect', '{}')")
        return real(conn, *args)

    monkeypatch.setattr(snapshots, "load_facts", writes_on_the_side)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        store.create(db, base["atlas"], base["a"], "tester")
    assert db.read_only is not True, "the connection is returned to its normal mode"
    assert db.execute("SELECT count(*) n FROM simulation").fetchone()["n"] == 0
    db.rollback()
    assert live_fingerprint(db) == before


def test_simulation_functions_never_commit_or_discard_the_callers_open_work(base):
    db = base["db"]
    db.execute("INSERT INTO buyer (id, name) VALUES ('pending-buyer', 'not yet committed')")
    with pytest.raises(RuntimeError, match="open transaction"):
        store.create(db, base["atlas"], base["a"], "tester")
    assert db.execute("SELECT count(*) n FROM buyer WHERE id = 'pending-buyer'").fetchone()["n"] == 1, "still the caller's to decide"
    db.rollback()


# --- The pure core: no database ------------------------------------------------------------------------------------------

def toy() -> Snapshot:
    facts = [Fact(1, "insurance", "Public liability", {"type": "public_liability", "amount_gbp": 5_000_000}, "document_on_file",
                  date(2027, 3, 31)),
             Fact(2, "capacity", "Up to 300 desks in 45 days", {"scope": "desks", "lead_days": 45, "max_units": 300}, "declared")]
    cond = lambda kind, fact, **kw: {"kind": kind, "fact": fact, **kw}  # noqa: E731
    reqs = [{"id": 10, "statement": "Insurance", "quote": "£5m", "category": "insurance", "obligation": "mandatory",
             "evidence_needed": [], "equivalent_allowed": False, "passage_id": 1, "baseline_verdict_id": 1, "baseline_status": "met",
             "conditions": [{"id": 1, "ordinal": 0, "label": "cover", "quote": "£5m", "mandatory": True,
                             "condition": cond("min_amount", "insurance.amount_gbp", bound=5_000_000, qualifier="public liability")}]}]
    return Snapshot(1, 1, 1, 1, date(2026, 11, 1), facts, reqs, {"notice.deadline": date(2026, 12, 1), "lot.quantity": 200.0})


def test_applying_changes_never_modifies_the_snapshot_and_is_deterministic():
    snap = toy()
    frozen = snap.to_json()
    changes = [{"kind": "set_condition_bound", "requirement_id": 10, "condition": 0, "bound": 10_000_000},
               {"kind": "set_fact_value", "fact_id": 1, "key": "amount_gbp", "value": 7_500_000},
               {"kind": "remove_fact", "fact_id": 2}]
    one, two = apply_changes(snap, changes), apply_changes(snap, changes)
    assert snap.to_json() == frozen
    assert (one.facts, one.requirements, one.fields) == (two.facts, two.requirements, two.fields)
    assert one.requirements[10]["conditions"][0]["condition"]["bound"] == 10_000_000
    assert [f.id for f in one.facts] == [1] and one.facts[0].value["amount_gbp"] == 7_500_000
    assert one.touched == {("requirement", "10"), ("fact", "1"), ("fact", "2")}


def test_snapshots_round_trip_and_their_hash_tracks_content():
    snap = toy()
    again = Snapshot.from_json(snap.to_json())
    assert again == snap and again.sha256() == snap.sha256()
    snap.facts[0].value["amount_gbp"] = 1
    assert snap.sha256() != again.sha256()


def test_added_facts_get_negative_ids_and_the_live_profile_rules():
    s = apply_changes(toy(), [
        {"kind": "add_fact", "attribute": "insurance", "label": "Employers liability", "value": {"type": "employer's liability",
                                                                                                "amount_gbp": 10_000_000}},
        {"kind": "add_fact", "attribute": "region", "label": "North East", "value": {"itl": ["UKC"]}, "evidence_status": "declared"}])
    added = [f for f in s.facts if f.id < 0]
    assert [f.id for f in added] == [-1, -2]
    assert added[0].value["type"] == "employers_liability", "normalised exactly as profile.service.clean_fact does"
    assert added[1].confirmed_at == date(2026, 11, 1) and added[1].confirm_by == date(2027, 11, 1), "declared on the as_of date"


def test_stored_changes_round_trip_exactly():
    for c in [{"kind": "set_notice_field", "field": "notice.deadline", "value": "2026-12-01"},
              {"kind": "set_fact_validity", "fact_id": 3, "valid_until": None},
              {"kind": "add_fact", "attribute": "certificate", "label": "ISO", "value": {"standard": "ISO 14001"}}]:
        parsed = parse_change(c)
        assert parse_change(dump_change(parsed)) == parsed
