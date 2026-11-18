"""The deterministic simulation API (Mode B, phase 1): routes, status codes, supplier isolation and live-data integrity.

Every request goes through the real session path (`as_supplier`). The integrity oracle is `live_fingerprint`: every row
of every non-simulation table and every non-simulation sequence. It is taken after the sessions are created, so any
difference is a write (or an attempted write) to live data by a simulation request."""
import threading

import pytest
from fastapi.testclient import TestClient

from tenderfit.api.main import app
from tenderfit.db import connect
from tenderfit.simulation import engine, store
from tests.conftest import as_supplier
from tests.test_isolation import world  # noqa: F401  (fixture)
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint, valid_changes

base = _base  # the fixture, shared with test_simulation.py

LEAD_45 = {"kind": "set_condition_bound", "requirement_id": None, "condition": 0, "bound": 45}


@pytest.fixture
def api(base):
    db = base["db"]
    a, b = as_supplier(db, base["atlas"]), as_supplier(db, base["pennine"])
    return {**base, "A": a, "B": b}


def lead(api, bound=45) -> dict:
    return {**LEAD_45, "requirement_id": api["lead"], "bound": bound}


def new(api, client="A", assessment=None, **body) -> dict:
    r = api[client].post("/simulations", json={"assessment_id": assessment or api["a" if client == "A" else "p"], **body})
    assert r.status_code == 201, r.text
    return r.json()


def routes(sid: int | str, lot: int | str) -> list[tuple[str, str, dict | None]]:
    """Every simulation route, as (method, path, body)."""
    return [("GET", f"/lots/{lot}/simulations", None), ("POST", "/simulations", {"assessment_id": 1}),
            ("GET", f"/simulations/{sid}", None), ("POST", f"/simulations/{sid}/changes", {"change": {"kind": "remove_fact", "fact_id": 1}}),
            ("GET", f"/simulations/{sid}/preview", None), ("POST", f"/simulations/{sid}/run", None),
            ("GET", f"/simulations/{sid}/results", None), ("POST", f"/simulations/{sid}/discard", None),
            ("GET", f"/simulations/{sid}/trace", None)]


# --- Routes -------------------------------------------------------------------------------------------------------------

def test_every_simulation_route_is_covered_here():
    """A new route without an entry in `routes` (and so without the auth, CSRF and isolation tests below) fails here."""
    published = {(m.upper(), p.removeprefix("/v1")) for p, ops in app.openapi()["paths"].items() if "simulation" in p for m in ops}
    assert published == {(m, p) for m, p, _ in routes("{simulation_id}", "{lot_id}")}


def test_no_promotion_route_exists():
    paths = " ".join(app.openapi()["paths"])
    for word in ("apply", "promote", "accept", "make-real", "commit"):
        assert word not in paths.split("simulations", 1)[-1], word


# --- The whole lifecycle, and nothing live changes ----------------------------------------------------------------------

def test_lifecycle_through_the_api_changes_nothing_live(api):
    db, A = api["db"], api["A"]
    before = live_fingerprint(db)
    assert A.get(f"/lots/{api['lot']}/simulations").json()["simulations"] == []

    sim = new(api, title="  What if 45 days?  ", as_of="2026-06-01")
    assert sim["hypothetical"] is True and "does not change your assessment" in sim["notice"]
    assert (sim["status"], sim["title"], sim["lot_id"], sim["as_of"]) == ("created", "What if 45 days?", api["lot"], "2026-06-01")
    assert sim["baseline"]["assessment_id"] == api["a"] and sim["baseline"]["superseded"] is False
    assert sim["created_by"] == "user%s@test.example" % api["atlas"], "who created it comes from the session"
    assert "snapshot" not in sim and sim["changes"] == [] and sim["summary"] is None and sim["failure"] is None
    sid = sim["id"]

    added = [A.post(f"/simulations/{sid}/changes", json={"change": c}) for c in valid_changes(api)]
    assert [r.status_code for r in added] == [201] * len(added), [r.text for r in added]
    assert [r.json()["ordinal"] for r in added] == list(range(1, len(added) + 1))
    assert [r.json().get("fact_id") for r in added if r.json()["change"]["kind"] == "add_fact"] == [-1]
    assert A.get(f"/simulations/{sid}").json()["changes"] == [r.json()["change"] for r in added]

    pv = A.get(f"/simulations/{sid}/preview").json()
    assert pv["hypothetical"] and {x["requirement_id"] for x in pv["reached"]} | {x["requirement_id"] for x in pv["not_reached"]} == \
        {api["lead"], api["quantity"], api["install"], api["standard"]}
    assert all(w["kind"] in ("direct", "used", "examines") for x in pv["reached"] for w in x["why"])

    ran = A.post(f"/simulations/{sid}/run")
    assert ran.status_code == 200, ran.text
    assert ran.json()["status"] == "completed" and ran.json()["summary"]["requirements"] == 4

    res = A.get(f"/simulations/{sid}/results").json()
    assert res["hypothetical"] and res["summary"] == ran.json()["summary"]
    stored = {r["requirement_id"]: r for r in store.results(db, api["atlas"], sid)}
    db.rollback()
    assert {r["requirement_id"]: (r["control_status"], r["simulated_status"], r["how"]) for r in res["results"]} == \
        {rid: (r["control_status"], r["simulated_status"], r["how"]) for rid, r in stored.items()}
    install = next(r for r in res["results"] if r["requirement_id"] == api["install"])
    assert install["how"] == "needs_model" and install["notes"], "new wording: only the model could read it, so it says so"

    listed = A.get(f"/lots/{api['lot']}/simulations").json()["simulations"]
    assert [(s["id"], s["status"], s["changes"]) for s in listed] == [(sid, "completed", len(added))]

    gone = A.post(f"/simulations/{sid}/discard")
    assert gone.status_code == 200 and gone.json()["status"] == "discarded" and gone.json()["discarded_at"]
    assert A.get(f"/lots/{api['lot']}/simulations").json()["simulations"] == []
    assert [s["id"] for s in A.get(f"/lots/{api['lot']}/simulations?include_discarded=true").json()["simulations"]] == [sid]
    assert A.get(f"/simulations/{sid}/results").status_code == 409, "a discarded simulation has no results to show"

    assert live_fingerprint(db) == before


def test_the_baseline_stays_frozen_while_the_live_profile_moves_on(api):
    db, A = api["db"], api["A"]
    sim = new(api)
    db.execute("UPDATE profile_fact SET value = value || '{\"lead_days\": 1}', updated_at = now() WHERE id = %s",
               (api["facts"]["capacity"],))
    db.commit()
    A.post(f"/simulations/{sim['id']}/changes", json={"change": lead(api)})
    done = A.post(f"/simulations/{sim['id']}/run").json()
    # The hash covers the frozen part only (facts, requirement wording and conditions, tender fields, date); live inputs
    # are tracked separately in summary.execution (tests/test_simulation_inputs.py).
    assert done["snapshot_sha256"] == sim["snapshot_sha256"], "the frozen part of the snapshot is unchanged"
    assert done["summary"]["execution"]["snapshot_sha256"] == sim["snapshot_sha256"]
    snap = store.get(db, api["atlas"], sim["id"])["snapshot"]
    db.rollback()
    capacity = next(f for f in snap["facts"] if f["id"] == api["facts"]["capacity"])
    assert capacity["value"].get("lead_days") != 1
    # A new simulation sees the edit as drift, and says so.
    assert new(api)["drift"]["facts_changed_since_baseline"] == [api["facts"]["capacity"]]


def test_reads_are_repeatable(api):
    A = api["A"]
    sid = new(api)["id"]
    A.post(f"/simulations/{sid}/changes", json={"change": lead(api)})
    assert A.get(f"/simulations/{sid}/preview").json() == A.get(f"/simulations/{sid}/preview").json()
    A.post(f"/simulations/{sid}/run")
    assert A.get(f"/simulations/{sid}/results").json() == A.get(f"/simulations/{sid}/results").json()
    assert A.get(f"/simulations/{sid}").json() == A.get(f"/simulations/{sid}").json()


def test_a_simulation_with_no_changes_runs_and_changes_nothing(api):
    sid = new(api)["id"]
    out = api["A"].post(f"/simulations/{sid}/run").json()
    assert out["summary"]["changed"] == [] and len(out["summary"]["not_affected"]) == 4


# --- Errors ---------------------------------------------------------------------------------------------------------------

def test_creating_from_an_assessment_that_cannot_be_a_baseline(api):
    db, A = api["db"], api["A"]
    assert A.post("/simulations", json={"assessment_id": 999999}).status_code == 404
    db.execute("UPDATE assessment SET status = 'failed' WHERE id = %s", (api["a"],))
    db.commit()
    r = A.post("/simulations", json={"assessment_id": api["a"]})
    assert r.status_code == 409 and "completed assessment" in r.json()["detail"]
    db.execute("UPDATE assessment SET status = 'done', shadow = true WHERE id = %s", (api["a"],))
    db.commit()
    r = A.post("/simulations", json={"assessment_id": api["a"]})
    assert r.status_code == 409 and "shadow" in r.json()["detail"]


@pytest.mark.parametrize("body", [
    {},
    {"assessment_id": "one"},
    {"assessment_id": 1, "profile_id": 2},  # the supplier never comes from the body
    {"assessment_id": 1, "title": "x" * 501},
    {"assessment_id": 1, "as_of": "not a date"},
])
def test_create_rejects_a_malformed_body(api, body):
    before = live_fingerprint(api["db"])
    assert api["A"].post("/simulations", json=body).status_code == 422
    assert api["A"].get(f"/lots/{api['lot']}/simulations").json()["simulations"] == []
    assert live_fingerprint(api["db"]) == before


def test_list_for_an_unknown_lot_and_limits(api):
    A = api["A"]
    assert A.get("/lots/999999/simulations").status_code == 404
    assert A.get(f"/lots/{api['lot']}/simulations?limit=0").status_code == 422
    assert A.get(f"/lots/{api['lot']}/simulations?limit=201").status_code == 422
    ids = [new(api)["id"] for _ in range(3)]
    assert [s["id"] for s in A.get(f"/lots/{api['lot']}/simulations?limit=2").json()["simulations"]] == ids[::-1][:2]


@pytest.mark.parametrize("change, says", [
    ({"kind": "make_it_pass"}, "invalid change"),
    ({"kind": "set_condition_bound", "requirement_id": 1, "condition": 0, "bound": -5}, "bound"),
    ({"kind": "set_condition_bound", "requirement_id": 1, "condition": 0, "bound": True}, "bound"),
    ({"kind": "set_notice_field", "field": "lot.quantity", "value": "2026-01-01"}, "lot.quantity takes a number"),
    ({"kind": "set_notice_field", "field": "lot.budget", "value": 5}, "field"),
    ({"kind": "set_fact_evidence", "fact_id": 1, "evidence_status": "verified_by_magic"}, "evidence_status"),
    ({"kind": "remove_fact", "fact_id": 1, "extra": "x"}, "extra"),
    ({"kind": "set_condition_bound", "requirement_id": 999999, "condition": 0, "bound": 5}, "not part of the baseline"),
    ({"kind": "set_fact_evidence", "fact_id": 999999, "evidence_status": "declared"}, "not in this supplier's profile"),
    ({"kind": "set_fact_evidence", "fact_id": -1, "evidence_status": "declared"}, "not in this supplier's profile"),
])
def test_an_invalid_or_unsupported_change_is_refused_and_not_recorded(api, change, says):
    A = api["A"]
    sid = new(api)["id"]
    r = A.post(f"/simulations/{sid}/changes", json={"change": change})
    assert r.status_code == 422 and says in r.json()["detail"], r.text
    assert A.get(f"/simulations/{sid}").json()["changes"] == []


def test_another_suppliers_fact_inside_a_change_is_refused(api):
    sid = new(api)["id"]
    r = api["A"].post(f"/simulations/{sid}/changes",
                      json={"change": {"kind": "set_fact_evidence", "fact_id": api["other_fact"], "evidence_status": "document_on_file"}})
    assert r.status_code == 422 and "not in this supplier's profile" in r.json()["detail"]


def test_change_body_must_be_an_object(api):
    sid = new(api)["id"]
    for body in ({"change": "remove everything"}, {"change": [1, 2]}, {}, {"change": {}, "profile_id": 2}):
        assert api["A"].post(f"/simulations/{sid}/changes", json=body).status_code == 422, body


def test_lifecycle_conflicts(api):
    A = api["A"]
    sid = new(api)["id"]
    assert A.get(f"/simulations/{sid}/results").status_code == 409, "not run yet"
    A.post(f"/simulations/{sid}/changes", json={"change": lead(api)})
    assert A.post(f"/simulations/{sid}/run").status_code == 200
    r = A.post(f"/simulations/{sid}/run")
    assert r.status_code == 409 and "runs once" in r.json()["detail"]
    r = A.post(f"/simulations/{sid}/changes", json={"change": lead(api, 50)})
    assert r.status_code == 409 and "before a simulation runs" in r.json()["detail"]
    assert A.post(f"/simulations/{sid}/discard").status_code == 200
    r = A.post(f"/simulations/{sid}/discard")
    assert r.status_code == 409 and "already discarded" in r.json()["detail"]

    other = new(api)["id"]
    assert A.post(f"/simulations/{other}/discard").status_code == 200, "a created simulation can be discarded"
    assert A.post(f"/simulations/{other}/run").status_code == 409
    assert A.post(f"/simulations/{other}/changes", json={"change": lead(api)}).status_code == 409


def test_a_running_simulation_cannot_be_discarded(api, _test_db):
    """While its run is alive (it holds the run lock on its own connection, as engine.run does), it cannot be discarded."""
    db = api["db"]
    sid = new(api)["id"]
    with connect(_test_db) as running:
        running.execute("SELECT pg_advisory_lock(%s, %s)", engine._lock_key(sid))
        running.execute("UPDATE simulation SET status = 'running', started_at = now() WHERE id = %s", (sid,))
        running.commit()
        r = api["A"].post(f"/simulations/{sid}/discard")
        assert r.status_code == 409 and "until it finishes" in r.json()["detail"]
        assert api["A"].get(f"/simulations/{sid}").json()["status"] == "running", "a live run is never closed"
    db.rollback()


def test_a_failed_run_is_marked_failed_and_its_internals_stay_private(api, monkeypatch):
    db, A = api["db"], api["A"]
    sid = new(api)["id"]
    before = live_fingerprint(db)

    def boom(*args, **kwargs):
        raise RuntimeError("password=hunter2 at 10.0.0.7")
    monkeypatch.setattr(engine, "_evaluate", boom)
    r = A.post(f"/simulations/{sid}/run")
    assert r.status_code == 500 and "marked failed" in r.json()["detail"] and "hunter2" not in r.text
    detail = A.get(f"/simulations/{sid}").json()
    assert detail["status"] == "failed" and detail["failure"]["stage"] == "evaluation"
    assert "hunter2" not in str(detail)
    assert "hunter2" in store.get(db, api["atlas"], sid)["error"], "the detail is kept for the operator"
    db.rollback()
    assert A.get(f"/simulations/{sid}/results").status_code == 409
    assert A.post(f"/simulations/{sid}/discard").status_code == 200, "a failed simulation can be discarded"
    assert live_fingerprint(db) == before


# --- Authentication, CSRF, supplier isolation ----------------------------------------------------------------------------

def test_every_route_needs_a_session(api):
    sid = new(api)["id"]
    anon = TestClient(app, base_url="http://testserver/v1", headers={"X-TenderFit": "1"})
    for method, path, body in routes(sid, api["lot"]):
        assert anon.request(method, path, json=body).status_code == 401, path


def test_every_change_needs_the_csrf_header(api):
    db = api["db"]
    sid = new(api)["id"]
    before = live_fingerprint(db)
    sims = store.get(db, api["atlas"], sid)
    db.rollback()
    for method, path, body in routes(sid, api["lot"]):
        if method == "POST":
            r = api["A"].request(method, path, json=body, headers={"X-TenderFit": ""})
            assert r.status_code == 403 and "X-TenderFit" in r.json()["detail"], path
    after = store.get(db, api["atlas"], sid)
    db.rollback()
    assert (after["status"], after["changes"]) == (sims["status"], sims["changes"]) == ("created", [])
    assert len(store.list_for_profile(db, api["atlas"])) == 1
    db.rollback()
    assert live_fingerprint(db) == before


def test_another_supplier_cannot_see_or_touch_a_simulation(api):
    db, A, B = api["db"], api["A"], api["B"]
    sim = new(api)
    sid = sim["id"]
    A.post(f"/simulations/{sid}/changes", json={"change": lead(api)})
    mine = A.get(f"/simulations/{sid}").json()
    before = live_fingerprint(db)
    for method, path, body in routes(sid, api["lot"]):
        if path.startswith("/lots/") or path == "/simulations":
            continue
        r = B.request(method, path, json=body)
        assert r.status_code == 404 and r.json()["detail"] == "simulation not found", (path, r.text)
    unknown = [B.request(m, p, json=b).json() for m, p, b in routes(999999, api["lot"]) if p.startswith("/simulations/")]
    known = [B.request(m, p, json=b).json() for m, p, b in routes(sid, api["lot"]) if p.startswith("/simulations/")]
    assert unknown == known, "another supplier's simulation is indistinguishable from a missing one"
    assert A.get(f"/simulations/{sid}").json() == mine, "nothing B tried changed it"
    assert live_fingerprint(db) == before


def test_another_suppliers_assessment_is_not_found(api):
    r = api["B"].post("/simulations", json={"assessment_id": api["a"]})
    assert r.status_code == 404 and r.json()["detail"] == "assessment not found"
    assert api["B"].post("/simulations", json={"assessment_id": 999999}).json() == r.json()


def test_each_supplier_lists_only_its_own(api):
    a, b = new(api)["id"], new(api, "B")["id"]
    assert [s["id"] for s in api["A"].get(f"/lots/{api['lot']}/simulations").json()["simulations"]] == [a]
    assert [s["id"] for s in api["B"].get(f"/lots/{api['lot']}/simulations?include_discarded=true").json()["simulations"]] == [b]


def test_a_user_without_a_supplier_is_refused(api):
    none = as_supplier(api["db"], None)
    assert none.get(f"/lots/{api['lot']}/simulations").status_code == 403
    assert none.post("/simulations", json={"assessment_id": api["a"]}).status_code == 403


# --- Concurrency and repeated requests ------------------------------------------------------------------------------------

def _together(calls) -> list:
    """Run the calls at the same moment, each on its own thread (and so its own request and database connection)."""
    gate, out = threading.Barrier(len(calls)), [None] * len(calls)

    def go(i, call):
        gate.wait()
        out[i] = call()
    threads = [threading.Thread(target=go, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_two_simultaneous_runs_start_exactly_one(api):
    db = api["db"]
    sid = new(api)["id"]
    api["A"].post(f"/simulations/{sid}/changes", json={"change": lead(api)})
    clients = [as_supplier(db, api["atlas"]) for _ in range(2)]
    before = live_fingerprint(db)
    codes = sorted(r.status_code for r in _together([lambda c=c: c.post(f"/simulations/{sid}/run") for c in clients]))
    assert codes == [200, 409]
    assert api["A"].get(f"/simulations/{sid}").json()["status"] == "completed"
    rows = store.results(db, api["atlas"], sid)
    db.rollback()
    assert len(rows) == 4, "one set of results, not two"
    assert live_fingerprint(db) == before


def test_simultaneous_changes_get_distinct_ordinals(api):
    db = api["db"]
    sid = new(api)["id"]
    clients = [as_supplier(db, api["atlas"]) for _ in range(4)]
    out = _together([lambda c=c, n=n: c.post(f"/simulations/{sid}/changes", json={"change": lead(api, 40 + n)})
                     for n, c in enumerate(clients)])
    assert [r.status_code for r in out] == [201] * 4
    assert sorted(r.json()["ordinal"] for r in out) == [1, 2, 3, 4]
    assert len(api["A"].get(f"/simulations/{sid}").json()["changes"]) == 4


def test_a_change_racing_a_run_is_either_in_the_run_or_refused(api):
    db = api["db"]
    sid = new(api)["id"]
    c1, c2 = as_supplier(db, api["atlas"]), as_supplier(db, api["atlas"])
    run, change = _together([lambda: c1.post(f"/simulations/{sid}/run"),
                             lambda: c2.post(f"/simulations/{sid}/changes", json={"change": lead(api)})])
    assert run.status_code == 200
    final = api["A"].get(f"/simulations/{sid}").json()
    if change.status_code == 201:
        assert final["changes"] == [change.json()["change"]], "added before the run started"
    else:
        assert change.status_code == 409 and final["changes"] == []


def test_double_submitted_create_makes_two_independent_drafts(api):
    """Creating is not idempotent by design: each is a separate draft, and either can be discarded."""
    first, second = new(api), new(api)
    assert first["id"] != second["id"] and first["snapshot_sha256"] == second["snapshot_sha256"]
