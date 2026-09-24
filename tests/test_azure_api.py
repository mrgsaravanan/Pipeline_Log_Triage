import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure_app import main  # noqa: E402
from Triage import Failure, Triage  # noqa: E402


def _client(monkeypatch, code=""):
    monkeypatch.setenv("TRIAGE_ACCESS_CODE", code)
    monkeypatch.setattr(main, "_append_history", lambda *a: None)
    monkeypatch.setattr(
        main, "run_triage",
        lambda name, text: Triage(
            failures=[Failure(failure_type="t", what_broke="w", evidence="e", next_step="n")],
            notes="ok",
        ),
    )
    return TestClient(main.app)


def test_api_triage_returns_result(monkeypatch):
    r = _client(monkeypatch).post("/api/triage", json={"text": "ERROR boom"})
    assert r.status_code == 200
    assert r.json()["notes"] == "ok"


def test_api_triage_rejects_wrong_access_code(monkeypatch):
    c = _client(monkeypatch, code="secret")
    assert c.post("/api/triage", json={"text": "x"}).status_code == 401
    ok = c.post("/api/triage", json={"text": "x"}, headers={"X-Access-Code": "secret"})
    assert ok.status_code == 200


def test_api_triage_requires_content(monkeypatch):
    assert _client(monkeypatch).post("/api/triage", json={"text": " "}).status_code == 400
