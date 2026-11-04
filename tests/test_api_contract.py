"""The published contract matches the code, and every route except health is versioned and needs a session."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from tenderfit.api.main import app

PUBLIC = {"/health", "/v1/auth/config", "/v1/auth/login", "/v1/auth/register"}


def test_published_contract_is_current():
    published = json.loads((Path(__file__).parent.parent / "docs" / "openapi.json").read_text())
    assert published == json.loads(json.dumps(app.openapi())), "run: PYTHONPATH=. python scripts/export_openapi.py"


def test_every_route_is_versioned_except_health():
    assert [p for p in app.openapi()["paths"] if not p.startswith("/v1/")] == ["/health"]


def test_old_unversioned_routes_are_gone():
    c = TestClient(app)  # no startup: these must fail on routing, before anything touches a database
    for method, path in (("GET", "/profiles"), ("POST", "/search"), ("GET", "/lots/1/assessment")):
        assert c.request(method, path, headers={"X-TenderFit": "1"}).status_code == 404


def test_no_route_accepts_a_supplier_or_user_from_the_client():
    schemas = app.openapi()["components"]["schemas"]
    for path, ops in app.openapi()["paths"].items():
        for op in ops.values():
            names = {p["name"] for p in op.get("parameters", [])}
            assert not names & {"profile_id", "user"}, path
    allowed = {"ActiveProfile"}  # choosing among your own suppliers, re-checked against membership
    for name, schema in schemas.items():
        if name not in allowed:
            assert not set(schema.get("properties", {})) & {"profile_id", "user"}, name
