"""The Vercel serverless entry (api/index.py) serves only the workflow routes."""

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

_spec = importlib.util.spec_from_file_location(
    "vercel_api_index", Path(__file__).resolve().parent.parent / "api" / "index.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
client = TestClient(_module.app)


def test_health():
    assert client.get("/api/health").json() == {"status": "ok"}


def test_me_requires_login():
    assert client.get("/api/me").status_code == 401


def test_triage_route_is_not_exposed():
    # Triage needs the claude CLI, which only exists on the owner's machine.
    assert client.post("/api/triage", json={"text": "x"}).status_code in (404, 405)


def test_public_stats_does_not_require_login():
    # Unlike every other data route, /api/public-stats is for the public About
    # page: no DATABASE_URL in this test env, so it 503s rather than 401ing -
    # the point is it's never gated on being signed in.
    assert client.get("/api/public-stats").status_code != 401
