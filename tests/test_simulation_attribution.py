"""Structural counterfactuals: bounded work, stable identities, honest uncertainty and no live writes."""
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tenderfit.rules.engine import Fact
from tenderfit.simulation import attribution, engine, store
from tenderfit.simulation.changes import SimulationError
from tenderfit.simulation.compare import Row
from tenderfit.simulation.scenario import apply_changes
from tenderfit.simulation.snapshot import Snapshot
from tests.test_isolation import world  # noqa: F401
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint
from tests.test_simulation_api import api as _api
from tests.test_simulation_engine import by_requirement, simulate
from tests.test_simulation_scenarios import compare, scenario, variation

base, api = _base, _api


def snap():
    return Snapshot(1, 1, 1, 1, date(2026, 6, 1), [
        Fact(i, "insurance", f"Policy {i}", {"amount_gbp": 1, "type": "public_liability"}, "document_on_file") for i in (1, 2)], [], {})


def decision(status, facts=None):
    return {"status": status, "reason": f"rule returned {status}", "facts": facts or [], "params": [],
            "citations": [{"passage_id": 1, "quote": "Both policies must cover the amount"}], "needs_human": None, "gaps": []}


def changes():
    return [{"kind": "set_fact_value", "fact_id": i, "key": "amount_gbp", "value": 10} for i in (1, 2)]


def row(rid=1):
    return Row(rid, "Insurance", "mandatory", 1, "not_met", decision("not_met"), decision("met"),
               [{"target": f"fact:{i}"} for i in (1, 2)])


def checker(kind):
    def check(state, rid):
        flags = [f.value["amount_gbp"] >= 10 for f in sorted(state.facts, key=lambda f: f.id)]
        passed = all(flags) if kind == "and" else any(flags) if kind == "or" else flags[0]
        return decision("met" if passed else "not_met", [f.id for f in state.facts])
    return check


@pytest.mark.parametrize("kind,expected", [
    ("and", [{"necessary", "jointly_required"}, {"necessary", "jointly_required"}]),
    ("or", [{"sufficient_alone", "redundant"}, {"sufficient_alone", "redundant"}]),
    ("first", [{"sufficient_alone", "necessary"}, {"redundant", "no_independent_effect"}]),
])
def test_checked_alternatives_explain_overlapping_properties(kind, expected):
    r = row()
    report = attribution.analyze([r], snap(), changes(), checker(kind))
    assert report["decisions"] == 4 and not report["limited"]
    entries = r.attribution["changes"]
    assert [set(e["labels"]) for e in entries] == expected
    assert all(e["alone"]["provenance"]["citations"] for e in entries)
    assert entries[1]["alone"]["provenance"]["facts"][1]["changes"][0]["ordinal"] == 2


@given(st.permutations([1, 2]), st.sampled_from(["and", "or", "first"]))
def test_commuting_changes_have_order_independent_results(order, kind):
    ordered = [changes()[i - 1] for i in order]
    r = row()
    attribution.analyze([r], snap(), ordered, checker(kind))
    assert all(e["state"] == "computed" for e in r.attribution["changes"])
    actual = {ordered[e["ordinal"] - 1]["fact_id"]: e["labels"] for e in r.attribution["changes"]}
    reference = row()
    attribution.analyze([reference], snap(), changes(), checker(kind))
    assert actual == {e["ordinal"]: e["labels"] for e in reference.attribution["changes"]}


def test_budget_is_shared_across_requirements_and_never_exceeded():
    rows = [row(1), row(2)]
    report = attribution.analyze(rows, snap(), changes(), checker("and"), limit=2)
    assert report["decisions"] == 2 and report["limited"]
    assert rows[0].attribution["changes"][0]["state"] == "computed"
    assert rows[0].attribution["changes"][1]["state"] == "limit"
    assert all(e["state"] == "limit" and not e["labels"] for e in rows[1].attribution["changes"])


def test_single_change_keeps_dependency_attribution_without_extra_calls():
    r = row()
    report = attribution.analyze([r], snap(), changes()[:1], lambda *_: pytest.fail("unexpected evaluation"))
    assert report["decisions"] == 0
    assert r.attribution["changes"][0]["state"] == "single_reaching_change"


def test_model_only_alternative_never_receives_a_classification():
    r = row()
    attribution.analyze([r], snap(), changes(), lambda *_: None)
    assert all(e["state"] == "undetermined" and e["labels"] == [] for e in r.attribution["changes"])
    assert r.attribution["changes"][0]["alone"]["state"] == "needs_model"


def additions():
    return [{"kind": "add_fact", "attribute": "insurance", "label": f"Hypothesis {i}", "value": {"amount_gbp": 10, "type": "public_liability"}}
            for i in (1, 2)] + [{"kind": "set_fact_value", "fact_id": -2, "key": "amount_gbp", "value": 20}]


def test_subsets_preserve_added_fact_identity_and_original_ordinals():
    s = apply_changes(snap(), additions(), include={2, 3})
    assert {f.id for f in s.facts} == {1, 2, -2}
    assert next(f for f in s.facts if f.id == -2).value["amount_gbp"] == 20
    assert s.change_ordinals == [2, 3]
    assert [e["ordinal"] for e in s.fact_changes[-2]] == [2, 3]
    with pytest.raises(SimulationError, match="fact -2"):
        apply_changes(snap(), additions(), include={1, 3})


def test_dependent_subsets_are_reported_not_repaired_or_mislabelled():
    r = row()
    r.reached = [{"target": "fact:-2"}]
    report = attribution.analyze([r], snap(), additions(), lambda s, rid: decision("met", [f.id for f in s.facts]))
    assert report["decisions"] == 2
    assert all(e["state"] == "undetermined" and e["labels"] == [] for e in r.attribution["changes"])
    assert r.attribution["changes"][0]["without"]["state"] == "invalid_subset"
    assert r.attribution["changes"][1]["alone"]["state"] == "invalid_subset"


def test_noncommuting_changes_are_not_reordered():
    edits = [{"kind": "set_fact_value", "fact_id": 1, "key": "amount_gbp", "value": n} for n in (10, 20)]
    assert apply_changes(snap(), edits).facts[0].value["amount_gbp"] == 20
    assert apply_changes(snap(), edits, include={1}).facts[0].value["amount_gbp"] == 10
    assert apply_changes(snap(), edits, include={2}).facts[0].value["amount_gbp"] == 20


def alternatives(b):
    return [{"kind": "set_condition_bound", "requirement_id": b["lead"], "condition": 0, "bound": 45},
            {"kind": "set_fact_value", "fact_id": b["facts"]["capacity"], "key": "lead_days", "value": 25}]


def test_real_counterfactuals_are_rules_only_reproducible_and_write_nothing_live(base, monkeypatch):
    db = base["db"]
    real_decide = engine._decide
    def checked(conn, *args, **kwargs):
        assert conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        assert conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "repeatable read"
        return real_decide(conn, *args, **kwargs)
    monkeypatch.setattr(engine, "_decide", checked)
    before, calls = live_fingerprint(db), len(base["llm"].users)
    sid = simulate(base, alternatives(base))
    done = engine.run(db, base["atlas"], sid, selective=False)
    entries = by_requirement(base, sid)[base["lead"]]["simulated"]["attribution"]["changes"]
    assert all(e["labels"] == ["sufficient_alone", "redundant"] for e in entries)
    assert done["summary"]["attribution"]["decisions"] == 4
    assert all(e["alone"]["provenance"]["citations"] for e in entries)
    assert len(base["llm"].users) == calls
    assert live_fingerprint(db) == before
    other = simulate(base, alternatives(base))
    engine.run(db, base["atlas"], other)
    assert by_requirement(base, other)[base["lead"]]["simulated"]["attribution"]["changes"] == entries


def test_results_and_comparison_expose_checked_alternatives_with_ownership(api):
    first = scenario(api, alternatives(api))
    second = variation(api, first["id"], alternatives(api))
    result = api["A"].get(f"/simulations/{first['id']}/results").json()
    assert next(r for r in result["results"] if r["requirement_id"] == api["lead"])["attribution"]["changes"]
    compared = compare(api, first["id"], second["id"])
    assert compared.status_code == 200
    body = compared.json()
    cell = next(r for r in body["requirements"] if r["requirement_id"] == api["lead"])["scenarios"][str(first["id"])]
    assert cell["attribution"]["changes"][0]["alone"]["status"] == "needs_confirmation"
    assert body["scenarios"][0]["changes"][0]["counterfactuals"][0]["requirement_id"] == api["lead"]
    assert compare(api, first["id"], second["id"], client="B").status_code == 404
    # Historical JSON has no attribution; readers return no invented counterfactuals.
    api["db"].rollback()
    old = store.results(api["db"], api["atlas"], first["id"])[0]
    old["simulated"].pop("attribution", None)
    from tenderfit.simulation.scenarios import _cell
    assert _cell(old)["attribution"] is None
