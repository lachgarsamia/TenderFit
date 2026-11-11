"""Replay must not destroy anything unless someone deliberately asks and names the database."""
import pytest

from tenderfit.assessment.pipeline import assess_lot
from tenderfit.documents import service
from tenderfit.ingest.importer import ReplayRefused, destructive_rebuild, rebuild_scope, replay, run_import
from tenderfit.profile.seed import seed
from tests import northfield
from tests.fakes import ScriptedLLM
from tests.test_importer import NOW, T0, T1, fake
from tests.test_pipeline import TODAY, portal, verdicts
from tests.test_retrieval import BagEmbedder, OverlapReranker


@pytest.fixture
def assessed(db, tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    run_import(db, fake([northfield.release()]), T0, T1, now=NOW)
    lot = db.execute("SELECT id FROM lot WHERE lot_no = '1'").fetchone()["id"]
    aid = assess_lot(db, lot, atlas, ScriptedLLM(verdicts), today=TODAY, emb=BagEmbedder(), rr=OverlapReranker(), agent=False,
                     http=portal(northfield.pdf(northfield.SPEC_V1)))
    return db, aid


TABLES = ("assessment", "verdict", "requirement", "document", "passage", "opportunity", "notice_version", "lot")


def counts(db):
    return {t: db.execute(f"SELECT count(*) n FROM {t}").fetchone()["n"] for t in TABLES}


def test_ordinary_replay_preserves_assessments(assessed):
    db, aid = assessed
    before = counts(db)
    assert all(before.values())
    assert replay(db, now=NOW)["applied"] > 0
    assert counts(db) == before
    assert db.execute("SELECT status, superseded FROM assessment WHERE id = %s", (aid,)).fetchone() == {"status": "done", "superseded": False}


def test_ordinary_replay_restores_what_is_missing(assessed):
    db, _ = assessed
    db.execute("UPDATE opportunity SET title = 'stale'")
    db.commit()
    replay(db, now=NOW)
    assert db.execute("SELECT title FROM opportunity").fetchone()["title"] != "stale"


@pytest.mark.parametrize("confirm", [None, "", "yes", "--force", "tenderfit", "TENDERFIT_TEST"])
def test_the_destructive_path_refuses_without_the_exact_database_name(assessed, confirm):
    db, _ = assessed
    before = counts(db)
    with pytest.raises(ReplayRefused):
        destructive_rebuild(db, confirm, now=NOW)
    assert counts(db) == before


def test_the_scope_is_visible_before_anything_happens(assessed):
    db, _ = assessed
    scope = rebuild_scope(db)
    assert scope["assessment"] == 1 and scope["verdict"] > 0 and scope["requirement"] > 0


def test_the_destructive_path_works_only_when_deliberately_named(assessed):
    db, _ = assessed
    name = db.execute("SELECT current_database() n").fetchone()["n"]
    destructive_rebuild(db, name, now=NOW)
    assert db.execute("SELECT count(*) n FROM assessment").fetchone()["n"] == 0
    assert db.execute("SELECT count(*) n FROM opportunity").fetchone()["n"] == 1


def test_no_api_route_or_worker_job_reaches_replay():
    import ast
    from pathlib import Path
    for path in [*Path("tenderfit/api").glob("*.py"), *Path("tenderfit/jobs").glob("*.py")]:
        names = {n.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.ImportFrom) for n in node.names}
        assert not names & {"replay", "destructive_rebuild"}, path


def test_the_command_line_needs_two_explicit_flags(monkeypatch, capsys):
    import sys

    from tenderfit.ingest import __main__ as cli
    monkeypatch.setattr(sys, "argv", ["x", "replay", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    out = capsys.readouterr().out
    assert "--destructive-rebuild" in out and "DESTRUCTIVE" in out and "--confirm-database" in out
