"""Scenario exploration (Mode B, phase 4): manual scenarios created in one request, variations on one frozen baseline, the
builder's inputs, and the deterministic comparison. Nothing live changes, ever: `live_fingerprint` is the oracle.

The Atlas fixture, as of TODAY: delivery (lead) is a mandatory blocker (not_met), quantity is met, installation is read by
the model only, and the standard is unknown (no BS EN 1729-2 test report)."""
import json
import re

import psycopg
import pytest

from tenderfit.assessment.pipeline import assess_lot
from tenderfit.db import MIGRATIONS
from tenderfit.simulation import scenarios, store
from tests.test_isolation import KW, world  # noqa: F401  (fixture)
from tests.test_pipeline import TODAY
from tests.test_simulation import live_fingerprint, valid_changes
from tests.test_simulation_api import api as _api
from tests.test_simulation_api import base as _base
from tests.test_simulation_api import lead

base, api = _base, _api  # the fixtures, shared with test_simulation_api.py
DAY = TODAY.isoformat()


def scenario(api, changes, client="A", title=None, run=True) -> dict:
    body = {"assessment_id": api["a" if client == "A" else "p"], "as_of": DAY, "changes": changes,
            **({"title": title} if title else {})}
    r = api[client].post("/simulations", json=body)
    assert r.status_code == 201, r.text
    if run:
        ran = api[client].post(f"/simulations/{r.json()['id']}/run")
        assert ran.status_code == 200, ran.text
        return ran.json()
    return r.json()


def variation(api, parent, changes, title=None, run=True) -> dict:
    r = api["A"].post(f"/simulations/{parent}/variations", json={"changes": changes, **({"title": title} if title else {})})
    assert r.status_code == 201, r.text
    if run:
        ran = api["A"].post(f"/simulations/{r.json()['id']}/run")
        assert ran.status_code == 200, ran.text
        return ran.json()
    return r.json()


def compare(api, *ids, client="A"):
    return api[client].get(f"/lots/{api['lot']}/simulations/compare", params={"ids": ",".join(map(str, ids))})


def remove_capacity(api) -> dict:
    return {"kind": "remove_fact", "fact_id": api["facts"]["capacity"]}


def sim_rows(db) -> int:
    n = db.execute("SELECT count(*) n FROM simulation").fetchone()["n"] + \
        db.execute("SELECT count(*) n FROM simulation_change").fetchone()["n"]
    db.rollback()
    return n


# --- A. Creating a scenario with its hypothesis, in one request ------------------------------------------------------------

def test_a_manual_scenario_is_created_with_all_its_changes_at_once(api):
    db = api["db"]
    before = live_fingerprint(db)
    sim = scenario(api, valid_changes(api), title="Everything at once", run=False)
    assert sim["hypothetical"] and sim["status"] == "created" and sim["parent_id"] is None
    assert [c["kind"] for c in sim["changes"]] == [c["kind"] for c in valid_changes(api)]
    stored = db.execute("SELECT ordinal FROM simulation_change WHERE simulation_id = %s ORDER BY ordinal", (sim["id"],)).fetchall()
    db.rollback()
    assert [r["ordinal"] for r in stored] == list(range(1, len(valid_changes(api)) + 1))
    assert api["A"].post(f"/simulations/{sim['id']}/run").json()["status"] == "completed"
    assert live_fingerprint(db) == before


@pytest.mark.parametrize("bad, says", [
    ({"kind": "set_condition_bound", "requirement_id": 999999, "condition": 0, "bound": 45}, "not part of the baseline"),
    ({"kind": "remove_fact", "fact_id": 999999}, "not in this supplier"),
    ({"kind": "set_condition_match", "requirement_id": "LEAD", "condition": 0, "match": "x"}, "change 2"),  # placeholder swapped below
    ({"kind": "promote_to_live"}, "change 2"),
    ({"kind": "set_fact_value", "fact_id": "CAP", "key": "lead_days", "value": "soon"}, "lead_days"),
    ({"kind": "add_fact", "attribute": "certificate", "label": "ISO 9001", "value": {"standard": "ISO 9001"},
      "evidence_status": "document_on_file", "profile_id": 2}, "change 2"),
])
def test_one_invalid_change_creates_nothing(api, bad, says):
    db = api["db"]
    bad = {k: (api["lead"] if v == "LEAD" else api["facts"]["capacity"] if v == "CAP" else v) for k, v in bad.items()}
    before, rows = live_fingerprint(db), sim_rows(db)
    r = api["A"].post("/simulations", json={"assessment_id": api["a"], "changes": [lead(api), bad]})
    assert r.status_code == 422 and says in r.text and "change 2" in r.text, r.text
    assert sim_rows(db) == rows, "nothing was created: the simulation and its changes are one transaction"
    assert live_fingerprint(db) == before


def test_a_protected_condition_cannot_be_changed(api):
    """Installation is read by the model only: it has no parameter a scenario may change."""
    r = api["A"].post("/simulations", json={"assessment_id": api["a"], "changes": [
        {"kind": "set_condition_bound", "requirement_id": api["install"], "condition": 0, "bound": 1}]})
    assert r.status_code == 422 and "not machine-checkable" in r.json()["detail"]


@pytest.mark.parametrize("changes", ["remove everything", [1, 2], [None], {"kind": "remove_fact"}])
def test_a_malformed_hypothesis_is_refused(api, changes):
    rows = sim_rows(api["db"])
    assert api["A"].post("/simulations", json={"assessment_id": api["a"], "changes": changes}).status_code == 422
    assert sim_rows(api["db"]) == rows


def test_size_limits(api):
    db = api["db"]
    rows = sim_rows(db)
    many = [lead(api, b) for b in range(1, store.MAX_CHANGES + 2)]
    assert api["A"].post("/simulations", json={"assessment_id": api["a"], "changes": many}).status_code == 422
    huge = {"kind": "add_fact", "attribute": "experience", "label": "x", "value": {"description": "y" * (store.MAX_CHANGE_BYTES + 1)}}
    r = api["A"].post("/simulations", json={"assessment_id": api["a"], "changes": [huge]})
    assert r.status_code == 422 and "too large" in r.json()["detail"], r.text
    assert sim_rows(db) == rows
    # Exactly at the limit is fine, and the one-at-a-time route holds the same line.
    full = scenario(api, many[:store.MAX_CHANGES], run=False)
    assert len(full["changes"]) == store.MAX_CHANGES
    r = api["A"].post(f"/simulations/{full['id']}/changes", json={"change": lead(api, 99)})
    assert r.status_code == 422 and f"at most {store.MAX_CHANGES}" in r.json()["detail"]
    r = api["A"].post(f"/simulations/{scenario(api, [], run=False)['id']}/changes", json={"change": huge})
    assert r.status_code == 422 and "too large" in r.json()["detail"]


def test_store_refuses_oversized_input_without_the_api(base):
    with pytest.raises(store.SimulationError, match="at most"):
        store.create(base["db"], base["atlas"], base["a"], "tester", changes=[{"kind": "remove_fact", "fact_id": 1}] * 26)
    with pytest.raises(store.SimulationError, match="list"):
        store.create(base["db"], base["atlas"], base["a"], "tester", changes={"kind": "remove_fact"})  # type: ignore[arg-type]


# --- B. Variations: one frozen baseline, many isolated hypotheses ----------------------------------------------------------

def test_a_variation_shares_its_parents_frozen_snapshot_and_leaves_the_parent_alone(api):
    db = api["db"]
    parent = scenario(api, [lead(api, 40)], title="40 days")
    before, kept = live_fingerprint(db), api["A"].get(f"/simulations/{parent['id']}").json()
    # The live profile moves on between the two; the variation is still decided on the parent's frozen copy.
    db.execute("UPDATE profile_fact SET value = value || '{\"lead_days\": 10}', updated_at = now() WHERE id = %s",
               (api["facts"]["capacity"],))
    db.commit()
    v = variation(api, parent["id"], [lead(api, 20)], title="20 days")
    assert (v["parent_id"], v["snapshot_sha256"], v["as_of"]) == (parent["id"], parent["snapshot_sha256"], parent["as_of"])
    assert v["baseline"] == parent["baseline"] and v["changes"] == [lead(api, 20)]
    assert api["A"].get(f"/simulations/{parent['id']}").json() == kept, "the parent is untouched"
    assert {s["id"]: s["parent_id"] for s in api["A"].get(f"/lots/{api['lot']}/simulations").json()["simulations"]} == \
        {parent["id"]: None, v["id"]: parent["id"]}
    # The variation's snapshot is the parent's byte for byte, not a new read of the (now different) live profile.
    a, b = (store.get(db, api["atlas"], x)["snapshot"] for x in (parent["id"], v["id"]))
    db.rollback()
    assert a == b
    after = live_fingerprint(db)
    assert {k for k in before if before[k] != after[k]} == {"profile_fact"}, "the test's own edit is the only live write"


def test_variations_of_variations_and_empty_variations(api):
    root = scenario(api, [lead(api, 40)])
    child = variation(api, root["id"], [lead(api, 30)])
    grandchild = variation(api, child["id"], [], run=False)
    assert grandchild["parent_id"] == child["id"] and grandchild["snapshot_sha256"] == root["snapshot_sha256"]
    assert grandchild["changes"] == [], "a variation carries only its own changes, never its parent's"


def test_a_variation_validates_its_changes_and_creates_nothing_when_one_is_wrong(api):
    db = api["db"]
    parent = scenario(api, [lead(api, 40)])
    rows = sim_rows(db)
    r = api["A"].post(f"/simulations/{parent['id']}/variations", json={"changes": [lead(api, 20), {"kind": "remove_fact", "fact_id": 999999}]})
    assert r.status_code == 422 and "change 2" in r.json()["detail"]
    r = api["A"].post(f"/simulations/{parent['id']}/variations", json={"changes": [lead(api)], "snapshot": {}})
    assert r.status_code == 422, "the snapshot always comes from the parent, never the request"
    assert sim_rows(db) == rows


def test_variation_conflicts_and_ownership(api):
    parent = scenario(api, [lead(api, 40)])
    assert api["B"].post(f"/simulations/{parent['id']}/variations", json={}).json() == \
        api["B"].post("/simulations/999999/variations", json={}).json() == {"detail": "simulation not found"}
    api["A"].post(f"/simulations/{parent['id']}/discard")
    r = api["A"].post(f"/simulations/{parent['id']}/variations", json={"changes": [lead(api, 20)]})
    assert r.status_code == 409 and "discarded" in r.json()["detail"]
    # A scenario that has not run yet can be varied too: its snapshot is already frozen.
    draft = scenario(api, [], run=False)
    assert api["A"].post(f"/simulations/{draft['id']}/variations", json={}).status_code == 201


def test_the_database_proves_a_variation_belongs_to_its_parent(api):
    """021's composite foreign key: same supplier, same baseline, same snapshot, or the row cannot exist."""
    db = api["db"]
    mine = scenario(api, [], run=False)
    theirs = scenario(api, [], client="B", run=False)
    cols = "profile_id, lot_id, baseline_assessment_id, version_id, as_of, snapshot, snapshot_sha256, drift, engine_version, created_by"
    for parent, sha in ((theirs["id"], mine["snapshot_sha256"]), (mine["id"], "0" * 64)):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            db.execute(f"INSERT INTO simulation ({cols}, parent_id) SELECT {cols.replace('snapshot_sha256', '%s')}, %s "
                       "FROM simulation WHERE id = %s", (sha, parent, mine["id"]))
        db.rollback()
    v = variation(api, mine["id"], [], run=False)
    with pytest.raises(psycopg.errors.RaiseException, match="cannot change"):
        db.execute("UPDATE simulation SET parent_id = NULL WHERE id = %s", (v["id"],))
    db.rollback()


def test_migration_021_touches_only_simulation_tables():
    sql = (MIGRATIONS / "021_simulation_variations.sql").read_text()
    code = "\n".join(line.split("--")[0] for line in sql.splitlines())
    touched = set(re.findall(r"\b(?:ALTER TABLE|INDEX \w+ ON|REFERENCES|UPDATE|INSERT INTO|DELETE FROM)\s+(\w+)", code))
    assert touched and all(t.startswith("simulation") for t in touched), touched


# --- The builder's inputs: actual values only -----------------------------------------------------------------------------

def test_inputs_show_actual_values_and_what_each_condition_accepts(api):
    db = api["db"]
    before, rows = live_fingerprint(db), sim_rows(db)
    r = api["A"].get(f"/lots/{api['lot']}/simulation-inputs", params={"assessment_id": api["a"]})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["hypothetical"] is False and got["assessment_id"] == api["a"]
    reqs = {x["id"]: x for x in got["requirements"]}
    assert reqs[api["lead"]]["conditions"][0]["accepts"] == ["set_condition_bound"]
    assert reqs[api["lead"]]["conditions"][0]["bound"] == 30
    assert reqs[api["standard"]]["conditions"][0]["accepts"] == ["set_condition_match"]
    assert reqs[api["install"]]["conditions"][0] | {} == {**reqs[api["install"]]["conditions"][0], "machine_checkable": False, "accepts": []}
    assert reqs[api["quantity"]]["conditions"][0]["accepts"] == [], "a bound read from the tender is changed via lot.quantity"
    assert all(f["id"] > 0 for f in got["facts"]), "only real facts: nothing hypothetical is listed as held"
    assert {f["id"] for f in got["facts"]} >= set(api["facts"].values())
    assert set(got["fields"]) == {"notice.deadline", "lot.quantity"} and "certificate" in got["fact_kinds"]
    assert sim_rows(db) == rows and live_fingerprint(db) == before, "reading the inputs creates nothing"


def test_inputs_ownership(api):
    lot = api["lot"]
    mine = api["A"].get(f"/lots/{lot}/simulation-inputs", params={"assessment_id": api["p"]})
    assert mine.status_code == 404 and mine.json()["detail"] == "assessment not found"
    assert api["A"].get("/lots/999999/simulation-inputs", params={"assessment_id": api["a"]}).status_code == 404
    assert api["A"].get(f"/lots/{lot}/simulation-inputs").status_code == 422
    sim = scenario(api, [lead(api, 40)], run=False)
    got = api["A"].get(f"/simulations/{sim['id']}/inputs").json()
    assert got["changes"] == [lead(api, 40)] and got["snapshot_sha256"] == sim["snapshot_sha256"]
    assert next(x for x in got["requirements"] if x["id"] == api["lead"])["conditions"][0]["bound"] == 30, \
        "the frozen value, not the hypothesis"
    assert api["B"].get(f"/simulations/{sim['id']}/inputs").json() == {"detail": "simulation not found"}


# --- C and D. Comparing scenarios -----------------------------------------------------------------------------------------

def test_comparison_names_every_transition_and_never_scores(api):
    db = api["db"]
    s45 = scenario(api, [lead(api, 45)], title="45 days")
    s1 = variation(api, s45["id"], [lead(api, 1)], title="1 day")
    gone = variation(api, s45["id"], [remove_capacity(api)], title="No capacity fact")
    before = live_fingerprint(db)
    r = compare(api, s45["id"], s1["id"], gone["id"])
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["hypothetical"] and "does not change" in c["notice"] and c["baseline_assessment_id"] == api["a"]
    assert c["integrity"] == {"same_snapshot": True, "same_live_inputs": True, "consistent": True, "notes": []}
    assert c["verdict_order"] == ["not_met", "unknown", "needs_confirmation", "met"]
    assert c["facts"][str(api["facts"]["capacity"])], "facts a change touches can be named"
    assert "score" not in json.dumps(c).replace("not a score", "")
    by = {s["id"]: s for s in c["scenarios"]}

    a = by[s45["id"]]
    assert a["counts"] == {"improved": 1, "worsened": 0, "unchanged": 3, "undetermined": 0}
    assert a["transitions"] == {"not_met -> needs_confirmation": 1}
    assert a["highlights"]["blockers_resolved"] == [api["lead"]] and a["highlights"]["now_needs_confirmation"] == [api["lead"]]
    assert a["changes"][0] | {} == {**a["changes"][0], "target": f"requirement:{api['lead']}", "reaches": [api["lead"]],
                                    "moved": [api["lead"]], "attributable": [api["lead"]], "assumes_evidence": False}

    b = by[s1["id"]]
    assert b["no_effect"] and b["counts"]["unchanged"] == 4 and b["transitions"] == {}
    assert b["changes"][0]["reaches"] == [api["lead"]] and b["changes"][0]["moved"] == [], "reached, but did not move"

    g = by[gone["id"]]
    assert g["transitions"] == {"not_met -> unknown": 1, "met -> unknown": 1}
    assert g["highlights"]["blockers_resolved"] == [], "a blocker that becomes unknown is not resolved"
    assert g["highlights"]["blockers_now_unknown"] == [api["lead"]]
    assert g["highlights"]["needs_model"] == [api["install"]] and g["counts"]["undetermined"] == 1

    assert c["order"] == [s45["id"], gone["id"], s1["id"]] and "not a score" in c["ordering_rule"]
    rows = {x["requirement_id"]: x for x in c["requirements"]}
    lead_row = rows[api["lead"]]
    assert lead_row["obligation"] == "mandatory" and lead_row["baseline_status"] == "not_met"
    assert lead_row["quote"] and lead_row["passage_id"], "each requirement carries its tender citation"
    assert {k: v["direction"] for k, v in lead_row["scenarios"].items()} == \
        {str(s45["id"]): "improved", str(s1["id"]): "unchanged", str(gone["id"]): "improved"}
    assert lead_row["differs_between_scenarios"] and lead_row["scenarios"][str(s45["id"])]["reason_simulated"]
    assert rows[api["install"]]["scenarios"][str(gone["id"])]["direction"] == "undetermined"
    assert not rows[api["standard"]]["affected"]

    p = c["priorities"]
    assert [(x["scenario_id"], x["ordinal"]) for x in p["addresses_blockers"]] == [(s45["id"], 1)]
    assert [(x["scenario_id"]) for x in p["little_or_no_effect"]] == [s1["id"]]
    assert p["widest_reach"][0]["scenario_id"] == gone["id"], "removing the capacity fact reaches three requirements"
    assert live_fingerprint(db) == before, "comparing writes nothing"


def test_uncertainty_evidence_and_blockers_introduced(api):
    report = {"kind": "add_fact", "attribute": "test_report", "label": "BS EN 1729-2 test report",
              "value": {"standard": "BS EN 1729-2"}, "evidence_status": "document_on_file"}
    more = {"kind": "set_notice_field", "field": "lot.quantity", "value": 100000}
    s = scenario(api, [report])
    t = variation(api, s["id"], [more])
    c = compare(api, s["id"], t["id"]).json()
    by = {x["id"]: x for x in c["scenarios"]}
    assert by[s["id"]]["highlights"]["uncertainties_resolved"] == [api["standard"]]
    assert by[s["id"]]["changes"][0]["target"] == "fact:-1" and by[s["id"]]["changes"][0]["assumes_evidence"] is True
    assert by[t["id"]]["highlights"]["blockers_introduced"] == [api["quantity"]]
    assert by[t["id"]]["transitions"] == {"met -> not_met": 1}
    p = c["priorities"]
    assert [x["scenario_id"] for x in p["resolves_uncertainty"]] == [s["id"]]
    assert [x["scenario_id"] for x in p["depends_on_evidence_not_held"]] == [s["id"]]
    assert [x["scenario_id"] for x in p["introduces_blockers"]] == [t["id"]]
    assert c["order"] == [s["id"], t["id"]]


def test_changes_that_share_a_requirement_are_not_attributed(api):
    s = scenario(api, [lead(api, 45), remove_capacity(api)])
    other = scenario(api, [])
    changes = {x["ordinal"]: x for x in compare(api, s["id"], other["id"]).json()["scenarios"][0]["changes"]}
    assert api["lead"] in changes[1]["shared_with_other_changes"] and api["lead"] not in changes[1]["attributable"]
    assert api["quantity"] in changes[2]["attributable"]


def test_separately_frozen_scenarios_are_compared_but_flagged(api):
    db = api["db"]
    first = scenario(api, [lead(api, 45)])
    db.execute("UPDATE profile_fact SET label = label || ' (renamed)', updated_at = now() WHERE id = %s", (api["facts"]["product"],))
    db.commit()
    second = scenario(api, [lead(api, 40)])
    c = compare(api, first["id"], second["id"]).json()
    assert c["integrity"]["same_snapshot"] is False and not c["integrity"]["consistent"]
    assert any("frozen separately" in n for n in c["integrity"]["notes"])


def test_a_live_change_between_runs_is_reported(api):
    db = api["db"]
    first = scenario(api, [lead(api, 45)])
    v = variation(api, first["id"], [lead(api, 40)], run=False)
    db.execute("UPDATE document_ref SET note = 'set aside by the supplier' WHERE id = (SELECT min(id) FROM document_ref)")
    db.commit()
    assert api["A"].post(f"/simulations/{v['id']}/run").status_code == 200
    c = compare(api, first["id"], v["id"]).json()
    assert c["integrity"]["same_snapshot"] is True, "still one frozen snapshot"
    assert c["integrity"]["same_live_inputs"] is False and not c["integrity"]["consistent"]
    notes = " ".join(c["integrity"]["notes"])
    assert "documents" in notes and f"scenario {v['id']}" in notes
    assert c["scenarios"][1]["input_drift"]["since_creation"], "and the scenario's own drift is listed"


def test_compare_refuses_what_it_cannot_compare(api, world):  # noqa: F811
    db, A = api["db"], api["A"]
    done = scenario(api, [lead(api, 45)])
    other = scenario(api, [lead(api, 40)])
    draft = scenario(api, [], run=False)
    assert compare(api, done["id"]).status_code == 422
    assert compare(api, done["id"], done["id"]).status_code == 422, "the same scenario twice is one scenario"
    assert compare(api, *[done["id"]] * 4, other["id"], draft["id"], 7, 8).status_code == 422
    assert A.get(f"/lots/{api['lot']}/simulations/compare", params={"ids": "1,two"}).status_code == 422
    assert A.get(f"/lots/{api['lot']}/simulations/compare", params={"ids": f"{done['id']},99999999999999999999"}).status_code == 422
    assert A.get(f"/lots/{api['lot']}/simulations/compare").status_code == 422
    r = compare(api, done["id"], draft["id"])
    assert r.status_code == 409 and "not completed" in r.json()["detail"]
    theirs = scenario(api, [], client="B")
    assert compare(api, done["id"], theirs["id"]).json() == compare(api, done["id"], 999999).json() == {"detail": "simulation not found"}
    assert compare(api, done["id"], other["id"], client="B").status_code == 404
    assert A.get("/lots/999999/simulations/compare", params={"ids": f"{done['id']},{other['id']}"}).status_code == 404
    A.post(f"/simulations/{other['id']}/discard")
    assert compare(api, done["id"], other["id"]).status_code == 409, "a discarded scenario is not compared"
    # Two assessments of the same lot are two baselines.
    _, atlas, _, lot, _, llm = world
    second = assess_lot(db, lot, atlas, llm, **KW)
    db.rollback()
    later = A.post("/simulations", json={"assessment_id": second, "as_of": DAY}).json()
    A.post(f"/simulations/{later['id']}/run")
    r = compare(api, done["id"], later["id"])
    assert r.status_code == 409 and "different assessments" in r.json()["detail"]


def test_direction_order():
    d = scenarios.direction
    assert [d("not_met", "unknown", "rule"), d("unknown", "needs_confirmation", "rule"), d("needs_confirmation", "met", "rule")] == \
        ["improved"] * 3
    assert d("met", "needs_confirmation", "rule") == "worsened" and d("met", "met", "rule") == "unchanged"
    assert d(None, None, "needs_model") == "undetermined" and d("met", "met", "not_affected") == "unchanged"
