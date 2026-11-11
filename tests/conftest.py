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
    except psycopg.OperationalError as e:
        if os.environ.get("REQUIRE_DB") or os.environ.get("CI"):
            pytest.fail(f"Postgres with pgvector is required here but unavailable: {e}")
        pytest.skip("Postgres not available")
    with connect(TEST_URL) as c:
        migrate(c)
    return TEST_URL


@pytest.fixture
def db(_test_db):
    with connect(_test_db) as c:
        c.execute("TRUNCATE login_failure, user_session, profile_member, app_user, passage_edge, fact_verification, document_ref_state, verdict_condition, requirement_condition, version_preparation, agent_step, change_impact, change_event, verdict_cache, lot_fact_confirmation, lot_decision, question, verdict_citation, verdict_fact, verdict, assessment, confirmation, llm_call, requirement_rejection, dependency, requirement, evidence_page, fact_support, evidence_file, profile_fact, business_profile, passage, document_ref, document, raw_release, buyer, opportunity, notice_version, lot, linked_notice, import_state, import_run, job RESTART IDENTITY CASCADE")
        c.commit()
        yield c


@pytest.fixture(autouse=True)
def _b2_baseline(monkeypatch, request):
    if "agent" not in request.keywords:
        monkeypatch.setattr("tenderfit.config.settings.agent_enabled", False)


def as_supplier(conn, profile_id: int | None, email: str | None = None):
    """A signed-in API client for one supplier, going through the same session code as a real login."""
    from fastapi.testclient import TestClient

    from tenderfit import auth
    from tenderfit.api.deps import COOKIE
    from tenderfit.api.main import app
    email = email or f"user{profile_id or 'none'}@test.example"
    row = conn.execute("SELECT id FROM app_user WHERE email = %s", (email,)).fetchone()
    uid = row["id"] if row else auth.create_user(conn, email, "correct horse battery")
    if profile_id is not None:
        auth.grant(conn, uid, profile_id)
    token = auth.create_session(conn, uid)
    conn.commit()
    client = TestClient(app, base_url="http://testserver/v1", headers={"X-TenderFit": "1"})
    client.cookies.set(COOKIE, token)
    return client
