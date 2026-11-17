"""Abandoned runs (review finding M1): a run whose process died is closed and can be discarded; a live run never is.

A run holds an advisory lock on its own database connection while it runs. These tests use real runs on real
connections: "the process died" is its database session being terminated mid-run, which is what Postgres sees when an
API process is killed."""
import threading

import psycopg
import pytest

from tenderfit.db import connect
from tenderfit.simulation import engine, store
from tenderfit.simulation.changes import SimulationConflict
from tests.conftest import as_supplier
from tests.test_isolation import world  # noqa: F401  (fixture)
from tests.test_simulation import base as _base
from tests.test_simulation import live_fingerprint
from tests.test_simulation_api import api as _api
from tests.test_simulation_api import lead, new

base, api = _base, _api  # fixtures, shared with test_simulation.py and test_simulation_api.py


class Blocked:
    """engine.run on its own connection, held inside evaluation (lock taken, status running) until released."""

    def __init__(self, url: str, profile_id: int, simulation_id: int, monkeypatch):
        self.started, self.release, self.result, self.error = threading.Event(), threading.Event(), None, None
        real = engine._evaluate

        def held(conn, *args, **kwargs):
            self.pid = conn.info.backend_pid
            self.started.set()
            assert self.release.wait(30)
            return real(conn, *args, **kwargs)
        monkeypatch.setattr(engine, "_evaluate", held)
        self.conn = connect(url)
        self.thread = threading.Thread(target=self._go, args=(profile_id, simulation_id))
        self.thread.start()
        assert self.started.wait(30), "the run did not start"

    def _go(self, profile_id, simulation_id):
        try:
            self.result = engine.run(self.conn, profile_id, simulation_id)
        except BaseException as e:  # noqa: BLE001  (reported to the test)
            self.error = e

    def finish(self):
        self.release.set()
        self.thread.join(30)
        if not self.conn.closed:
            self.conn.close()


def status(db, profile_id, sid) -> dict:
    row = store.get(db, profile_id, sid)
    db.rollback()
    return row


def test_a_run_whose_process_died_is_closed_and_can_then_be_discarded(api, _test_db, monkeypatch):
    """The finding, reproduced: the run's session dies mid-run, so nothing marks it failed. Before the fix it stayed
    `running` for ever and every discard got 409."""
    db, A = api["db"], api["A"]
    sid = new(api)["id"]
    A.post(f"/simulations/{sid}/changes", json={"change": lead(api)})
    before = live_fingerprint(db)
    run = Blocked(_test_db, api["atlas"], sid, monkeypatch)
    db.execute("SELECT pg_terminate_backend(%s)", (run.pid,))
    db.commit()
    run.finish()
    assert isinstance(run.error, psycopg.OperationalError), "the run lost its connection"
    assert status(db, api["atlas"], sid)["status"] == "running", "nothing could mark it failed"

    detail = A.get(f"/simulations/{sid}").json()
    assert detail["status"] == "failed" and detail["failure"]["stage"] == "interrupted"
    assert A.post(f"/simulations/{sid}/discard").status_code == 200
    assert live_fingerprint(db) == before


def test_a_run_that_is_still_working_is_never_closed(api, _test_db, monkeypatch):
    db, A = api["db"], api["A"]
    sid = new(api)["id"]
    A.post(f"/simulations/{sid}/changes", json={"change": lead(api)})
    run = Blocked(_test_db, api["atlas"], sid, monkeypatch)
    try:
        db.execute("UPDATE simulation SET started_at = now() - interval '3 hours' WHERE id = %s", (sid,))  # as if very slow
        db.commit()
        assert engine.close_abandoned_runs(db) == 0
        assert engine.recover_stale_simulations(db, minutes=1) == 0, "the periodic sweep spares it too, however old"
        assert A.get(f"/simulations/{sid}").json()["status"] == "running"
        r = A.post(f"/simulations/{sid}/discard")
        assert r.status_code == 409 and "until it finishes" in r.json()["detail"]
        with pytest.raises(SimulationConflict, match="runs once; this one is running"):
            engine.run(db, api["atlas"], sid)
    finally:
        run.finish()
    assert run.error is None and run.result["status"] == "completed", "the live run finished normally"
    assert A.post(f"/simulations/{sid}/discard").status_code == 200


def test_concurrent_recoveries_close_each_abandoned_run_once(api, _test_db):
    db = api["db"]
    sids = [new(api)["id"] for _ in range(5)]
    db.execute("UPDATE simulation SET status = 'running', started_at = now() WHERE id = ANY(%s)", (sids,))  # no live run
    db.commit()
    conns = [connect(_test_db) for _ in range(3)]
    gate, closed = threading.Barrier(len(conns)), [0] * len(conns)

    def go(i):
        gate.wait()
        closed[i] = engine.close_abandoned_runs(conns[i], api["atlas"])
    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(conns))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for c in conns:
        c.close()
    assert sum(closed) == 5
    assert {status(db, api["atlas"], s)["status"] for s in sids} == {"failed"}


def test_recovery_is_scoped_to_the_supplier(api):
    db = api["db"]
    sid = new(api)["id"]
    db.execute("UPDATE simulation SET status = 'running', started_at = now() WHERE id = %s", (sid,))
    db.commit()
    api["B"].get(f"/lots/{api['lot']}/simulations")
    assert api["B"].get(f"/simulations/{sid}").status_code == 404
    assert status(db, api["atlas"], sid)["status"] == "running", "another supplier's requests never touch it"
    assert [s["status"] for s in api["A"].get(f"/lots/{api['lot']}/simulations").json()["simulations"]] == ["failed"]


def test_a_lock_is_released_after_every_run(api, _test_db, monkeypatch):
    """Completed, refused and failed runs alike leave no lock behind on a connection that stays open (as the agent's
    does; an API request's connection closes anyway), so recovery and later runs are never blocked."""
    db = api["db"]
    ok, bad = new(api)["id"], new(api)["id"]
    assert engine.run(db, api["atlas"], ok)["status"] == "completed"
    with pytest.raises(SimulationConflict):
        engine.run(db, api["atlas"], ok)

    def boom(*args, **kwargs):
        raise RuntimeError("evaluation failed")
    monkeypatch.setattr(engine, "_evaluate", boom)
    with pytest.raises(RuntimeError):
        engine.run(db, api["atlas"], bad)
    assert status(db, api["atlas"], bad)["status"] == "failed"
    with connect(_test_db) as other:
        for sid in (ok, bad):
            got = other.execute("SELECT pg_try_advisory_lock(%s, %s) AS got", engine._lock_key(sid)).fetchone()["got"]
            assert got, sid
            other.execute("SELECT pg_advisory_unlock(%s, %s)", engine._lock_key(sid))


def test_a_second_client_sees_the_same_recovery(api):
    """Two signed-in sessions of the same supplier: whichever reads first closes the abandoned run, the other sees it."""
    db = api["db"]
    sid = new(api)["id"]
    db.execute("UPDATE simulation SET status = 'running', started_at = now() WHERE id = %s", (sid,))
    db.commit()
    other = as_supplier(db, api["atlas"])
    first, second = api["A"].get(f"/simulations/{sid}").json(), other.get(f"/simulations/{sid}").json()
    assert first["status"] == second["status"] == "failed" and first["finished_at"] == second["finished_at"]
