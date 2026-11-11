"""The boundary itself: who is signed in, and which supplier they act for."""
import pytest
from fastapi.testclient import TestClient

from tenderfit import auth
from tenderfit.api.deps import COOKIE
from tenderfit.api.main import app
from tenderfit.profile.seed import seed
from tests.conftest import as_supplier

PASSWORD = "correct horse battery"


@pytest.fixture
def env(db, _test_db, monkeypatch):
    monkeypatch.setattr("tenderfit.config.settings.database_url", _test_db)
    monkeypatch.setattr("tenderfit.config.settings.session_cookie_secure", False)
    return db


def anon():
    return TestClient(app, base_url="http://testserver/v1", headers={"X-TenderFit": "1"})


# Every route that holds or changes supplier or tender data, with a body where it needs one.
PROTECTED = [("get", "/profiles"), ("post", "/profiles"), ("put", "/profile"), ("get", "/profile/facts"), ("post", "/profile/facts"), ("put", "/profile/facts/1"),
             ("post", "/profile/evidence"), ("post", "/profile/facts/1/renew"), ("post", "/profile/facts/1/verify"), ("get", "/profile/evidence-impact"), ("get", "/profile/pursued"),
             ("get", "/opportunities"), ("post", "/search"), ("get", "/tenders/1"), ("get", "/lots/1"), ("get", "/lots/1/assessment"), ("post", "/lots/1/assess"),
             ("get", "/lots/1/documents"), ("post", "/lots/1/documents"), ("get", "/lots/1/preparation"), ("get", "/changes"), ("post", "/changes/1/retry"),
             ("post", "/confirmations"), ("post", "/questions/1/answer"), ("get", "/jobs/1"), ("get", "/assessments/1/trace"), ("get", "/passages/1"),
             ("get", "/documents/1/file"), ("get", "/import-status"), ("get", "/auth/me"), ("post", "/auth/logout"), ("post", "/auth/active-profile")]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_route_needs_a_signed_in_user(env, method, path):
    with anon() as c:
        assert getattr(c, method)(path).status_code == 401, path


def test_no_route_was_left_out_of_that_list():
    public = {"/health", "/v1/auth/config", "/v1/auth/login", "/v1/auth/register"}
    declared = {(m, p) for p, ops in app.openapi()["paths"].items() for m in ops}
    covered = {(m, "/v1" + p.replace("/1", "/{}")) for m, p in PROTECTED}
    missing = [(m, p) for m, p in declared if p not in public and (m, __import__("re").sub(r"\{[a-z_]+\}", "{}", p)) not in covered]
    assert not missing, missing


def test_unversioned_routes_are_gone(env):
    with TestClient(app) as c:
        assert c.get("/profile/facts", params={"profile_id": 1}).status_code == 404
        assert c.get("/opportunities").status_code == 404


def test_changing_data_needs_the_custom_header(env):
    pid = seed(env)["Atlas Furniture"]
    with as_supplier(env, pid) as c:
        del c.headers["X-TenderFit"]
        assert c.post("/lots/1/assess").status_code == 403, "a cross-site form cannot set this header"
        assert c.get("/profile/facts").status_code == 200


def test_login_session_logout(env):
    pid = seed(env)["Atlas Furniture"]
    uid = auth.create_user(env, "a@test.example", PASSWORD)
    auth.grant(env, uid, pid)
    env.commit()
    assert PASSWORD not in env.execute("SELECT password_hash FROM app_user").fetchone()["password_hash"]
    with anon() as c:
        assert c.post("/auth/login", json={"email": "a@test.example", "password": "wrong password"}).status_code == 401
        r = c.post("/auth/login", json={"email": "A@test.example", "password": PASSWORD})
        assert r.status_code == 200 and r.json()["active_profile_id"] == pid, "one supplier: selected automatically"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        token = c.cookies[COOKIE]
        assert env.execute("SELECT count(*) n FROM user_session WHERE token_hash = %s", (token,)).fetchone()["n"] == 0, "the raw token is not stored"
        assert c.get("/auth/me").json()["email"] == "a@test.example"
        assert c.post("/auth/logout").status_code == 200
    with anon() as c:
        c.cookies.set(COOKIE, token)
        assert c.get("/auth/me").status_code == 401, "a signed-out token is dead"


def test_the_secure_flag_is_on_by_default():
    from tenderfit.config import Settings
    assert Settings.model_fields["session_cookie_secure"].default is True
    assert Settings.model_fields["registration_enabled"].default is False


def test_wrong_password_and_unknown_account_look_the_same(env):
    auth.create_user(env, "a@test.example", PASSWORD)
    env.commit()
    with anon() as c:
        a = c.post("/auth/login", json={"email": "a@test.example", "password": "wrong password"})
        b = c.post("/auth/login", json={"email": "nobody@test.example", "password": "wrong password"})
    assert (a.status_code, a.json()) == (b.status_code, b.json())


def test_repeated_failures_lock_the_login_even_with_the_right_password(env, monkeypatch):
    monkeypatch.setattr("tenderfit.config.settings.login_max_failures", 3)
    auth.create_user(env, "a@test.example", PASSWORD)
    env.commit()
    with anon() as c:
        for _ in range(3):
            c.post("/auth/login", json={"email": "a@test.example", "password": "wrong password"})
        assert c.post("/auth/login", json={"email": "a@test.example", "password": PASSWORD}).status_code == 401


def test_a_disabled_account_and_an_expired_session_stop_working(env):
    pid = seed(env)["Atlas Furniture"]
    with as_supplier(env, pid) as c:
        assert c.get("/profile/facts").status_code == 200
        env.execute("UPDATE user_session SET expires_at = now() - interval '1 minute'")
        env.commit()
        assert c.get("/profile/facts").status_code == 401
    with as_supplier(env, pid) as c:
        env.execute("UPDATE app_user SET disabled_at = now()")
        env.commit()
        assert c.get("/profile/facts").status_code == 401


def test_removing_a_membership_takes_effect_on_the_next_request(env):
    pid = seed(env)["Atlas Furniture"]
    with as_supplier(env, pid) as c:
        assert c.get("/profile/facts").status_code == 200
        env.execute("DELETE FROM profile_member")
        env.commit()
        r = c.get("/profile/facts")
        assert r.status_code == 403 and "choose or create" in r.json()["detail"]


def test_active_profile_can_only_be_one_you_belong_to(env):
    ids = seed(env)
    with as_supplier(env, ids["Atlas Furniture"]) as c:
        assert c.post("/auth/active-profile", json={"profile_id": ids["Pennine Seating"]}).status_code == 404
        assert c.post("/auth/active-profile", json={"profile_id": 999999}).status_code == 404
        assert [p["id"] for p in c.get("/profiles").json()] == [ids["Atlas Furniture"]], "other suppliers are not listed"
        assert c.get("/auth/me").json()["active_profile_id"] == ids["Atlas Furniture"]


def test_signed_in_without_a_supplier_can_only_create_one(env):
    with as_supplier(env, None, "new@test.example") as c:
        assert c.get("/profile/facts").status_code == 403
        assert c.post("/search", json={}).status_code == 403
        assert c.get("/auth/me").json()["profiles"] == []
        r = c.post("/profiles", json={"name": "New Supplier Ltd", "regions": ["UKE"]})
        assert r.status_code == 201 and r.json()["active_profile_id"]
        assert c.get("/profile/facts").json() == []


def test_registration_is_off_by_default(env, monkeypatch):
    with anon() as c:
        assert c.post("/auth/register", json={"email": "x@test.example", "password": PASSWORD}).status_code == 403
        monkeypatch.setattr("tenderfit.config.settings.registration_enabled", True)
        assert c.post("/auth/register", json={"email": "x@test.example", "password": "short"}).status_code == 422
        assert c.post("/auth/register", json={"email": "x@test.example", "password": PASSWORD}).status_code == 201
        assert c.post("/auth/register", json={"email": "X@test.example", "password": PASSWORD}).status_code == 422


def test_health_says_nothing_about_data(env):
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}
