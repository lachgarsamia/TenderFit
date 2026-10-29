import os

import psycopg
import pytest

from tenderfit.db import connect, migrate

ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL", "postgresql://tenderfit:tenderfit@localhost:5432/postgres")
TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://tenderfit:tenderfit@localhost:5432/tenderfit_test")


@pytest.fixture(scope="session")
def _test_db():
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3) as admin:
            admin.execute("DROP DATABASE IF EXISTS tenderfit_test WITH (FORCE)")
            admin.execute("CREATE DATABASE tenderfit_test")
    except psycopg.OperationalError:
        pytest.skip("Postgres not available")
    with connect(TEST_URL) as c:
        migrate(c)
    return TEST_URL


@pytest.fixture
def db(_test_db):
    with connect(_test_db) as c:
        c.execute("TRUNCATE agent_step, change_impact, change_event, verdict_cache, lot_fact_confirmation, lot_decision, question, verdict_citation, verdict_fact, verdict, assessment, confirmation, llm_call, requirement_rejection, dependency, requirement, evidence_page, fact_support, evidence_file, profile_fact, business_profile, passage, document_ref, document, raw_release, buyer, opportunity, notice_version, lot, linked_notice, import_state, import_run, job RESTART IDENTITY CASCADE")
        c.commit()
        yield c


@pytest.fixture(autouse=True)
def _b2_baseline(monkeypatch, request):
    if "agent" not in request.keywords:
        monkeypatch.setattr("tenderfit.config.settings.agent_enabled", False)
