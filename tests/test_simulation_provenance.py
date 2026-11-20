"""Frozen support, honest assumptions, legacy hashes, source authorisation and unchanged live state."""
import hashlib
import json
from datetime import date

from tenderfit.rules.engine import Fact
from tenderfit.simulation import engine, provenance, store
from tenderfit.simulation.scenario import apply_changes
from tenderfit.simulation.snapshot import Snapshot, take_snapshot
from tests.test_isolation import world  # noqa: F401
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint, valid_changes
from tests.test_simulation_api import api as _api
from tests.test_simulation_api import new
from tests.test_simulation_engine import by_requirement, simulate

base = _base
api = _api

def test_legacy_json_round_trip_preserves_the_hash():
    snap = Snapshot(1, 1, 1, 1, date(2026, 6, 1), [Fact(1, "insurance", "Cover", {"amount_gbp": 5}, "declared")], [], {})
    old = snap.to_json()
    assert "schema_version" not in old and "support" not in old["facts"][0]
    digest = hashlib.sha256(json.dumps(old, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert Snapshot.from_json(old).sha256() == digest
    snap.support = {1: []}
    assert Snapshot.from_json(snap.to_json()).support == {1: []}
    snap.facts = []
    assert Snapshot.from_json(snap.to_json()).support == {}


def test_origins_cover_all_change_kinds_and_keep_ordered_history(base):
    db = base["db"]
    snap, _ = take_snapshot(db, base["atlas"], base["a"])
    db.rollback()
    original = snap.sha256()
    s = apply_changes(snap, valid_changes(base))
    d = {"facts": [f.id for f in snap.facts] + [-1], "params": ["lot.quantity"], "citations": [], "needs_human": False, "gaps": []}
    record = provenance.record(d, s, s, base["lead"])
    facts = {f["id"]: f for f in record["facts"]}
    assert facts[-1]["origin"] == "hypothetical" and facts[-1]["support"] == []
    assert [c["ordinal"] for c in facts[-1]["changes"]] == [6, 7]
    assert facts[base["facts"]["insurance"]]["origin"] == "modified"
    assert [c["ordinal"] for c in facts[base["facts"]["insurance"]]["changes"]] == [3, 5]
    assert facts[base["facts"]["capacity"]]["origin"] == "actual"
    control = provenance.record(d, apply_changes(snap, []), s, base["lead"], control=True)
    assert next(f for f in control["facts"] if f["id"] == base["facts"]["service"])["origin"] == "removed"
    assert record["params"][0]["origin"] == "modified"
    assert record["requirement_assumptions"][0]["ordinal"] == 1
    assert snap.sha256() == original


def test_rule_results_keep_citations_and_selective_equivalence(base):
    db = base["db"]
    before = live_fingerprint(db)
    changes = [{"kind": "set_condition_bound", "requirement_id": base["lead"], "condition": 0, "bound": 45}]
    sid = simulate(base, changes)
    engine.run(db, base["atlas"], sid, selective=False)
    rows = by_requirement(base, sid)
    for row in rows.values():
        for mode in ("control", "simulated"):
            rec = row["simulated"][mode]
            if rec:
                assert rec["provenance"]["citations"] == rec["citations"] and rec["citations"]
                assert {f["id"] for f in rec["provenance"]["facts"]} == set(rec["facts"])
    assert live_fingerprint(db) == before


def test_support_is_frozen_even_if_live_verification_changes(base):
    db = base["db"]
    fid = base["facts"]["capacity"]
    eid = db.execute("""INSERT INTO evidence_file (profile_id, kind, filename, sha256)
                        VALUES (%s, 'capacity', 'capacity.pdf', 'frozen-proof') RETURNING id""", (base["atlas"],)).fetchone()["id"]
    db.execute("INSERT INTO fact_support (fact_id, evidence_file_id, page, quote, verified) VALUES (%s, %s, 1, '300 desks', true)", (fid, eid))
    db.execute("INSERT INTO fact_verification (fact_id, evidence_file_id, level, checks) VALUES (%s, %s, 'supports', '{}')", (fid, eid))
    db.commit()
    sid = simulate(base, [])
    db.execute("UPDATE fact_verification SET level = 'relevant' WHERE fact_id = %s", (fid,))
    db.execute("UPDATE fact_support SET quote = 'changed', verified = false WHERE fact_id = %s", (fid,))
    db.commit()
    before = live_fingerprint(db)
    engine.run(db, base["atlas"], sid)
    rec = by_requirement(base, sid)[base["quantity"]]["simulated"]["simulated"]["provenance"]
    support = next(f for f in rec["facts"] if f["id"] == fid)["support"][0]
    assert support["quote"] == "300 desks" and support["verified"] is True
    assert support["verification_level"] == "supports"
    assert live_fingerprint(db) == before


def test_provenance_route_scopes_result_and_sources(api):
    sim = new(api, changes=[{"kind": "set_condition_bound", "requirement_id": api["lead"], "condition": 0, "bound": 45}])
    sid = sim["id"]
    path = f"/simulations/{sid}/results/{api['lead']}/provenance"
    assert api["A"].get(path).status_code == 409
    assert api["A"].post(f"/simulations/{sid}/run").status_code == 200
    before = live_fingerprint(api["db"])
    result = api["A"].get(path)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["hypothetical"] and body["simulated"]["citations"] and body["passages"]
    assert api["B"].get(path).status_code == 404
    assert api["A"].get(f"/simulations/{sid}/results/999999/provenance").status_code == 404
    assert live_fingerprint(api["db"]) == before
    # Revoke source visibility: the result remains, but another supplier's document text must not resolve.
    pid = body["simulated"]["citations"][0]["passage_id"]
    api["db"].execute("UPDATE document SET owner_profile_id = %s WHERE id = (SELECT document_id FROM passage WHERE id = %s)", (api["pennine"], pid))
    api["db"].commit()
    assert api["A"].get(path).json()["passages"][str(pid)] is None


def test_legacy_runs_and_variations_keep_the_original_snapshot(base):
    db = base["db"]
    snap, drift = take_snapshot(db, base["atlas"], base["a"])
    db.rollback()
    snap.support = None
    sid = store._insert(db, base["atlas"], snap, snap.sha256(), drift, "legacy", "tester", [], None, engine_version="sim-v1")
    engine.run(db, base["atlas"], sid)
    rows = by_requirement(base, sid)
    rec = rows[base["lead"]]["simulated"]["simulated"]["provenance"]
    assert rec["support_available"] is False and all(f["support"] == [] for f in rec["facts"])
    variation = store.create_variation(db, base["atlas"], sid, "tester")
    v = store.get(db, base["atlas"], variation)
    db.rollback()
    assert v["engine_version"] == "sim-v1" and v["snapshot_sha256"] == snap.sha256()
    engine.run(db, base["atlas"], variation)


def test_repeated_parameter_changes_keep_each_intermediate_value():
    snap = Snapshot(1, 1, 1, 1, date(2026, 6, 1), [], [], {"lot.quantity": 200})
    s = apply_changes(snap, [{"kind": "set_notice_field", "field": "lot.quantity", "value": 300},
                             {"kind": "set_notice_field", "field": "lot.quantity", "value": 400}])
    d = {"facts": [], "params": ["lot.quantity"], "citations": [], "needs_human": None, "gaps": []}
    param = provenance.record(d, s, s, 1)["params"][0]
    assert param["changes"] == [{"ordinal": 1, "before": 200, "after": 300}, {"ordinal": 2, "before": 300, "after": 400}]
