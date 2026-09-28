import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adf  # noqa: E402
import local_server  # noqa: E402
from Triage import Failure, Triage  # noqa: E402

RUN = {"pipelineName": "CopySales", "runId": "r1", "status": "Failed", "message": "Activity failed"}
ACTS = [
    {"activityName": "Copy1", "activityType": "Copy", "status": "Failed",
     "error": {"errorCode": "2200", "message": "Timeout"}},
    {"activityName": "Ok", "activityType": "Lookup", "status": "Succeeded"},
]


def test_build_log_text_includes_only_failed_activities():
    text = adf.build_log_text(RUN, ACTS)
    assert "CopySales" in text and "Copy1" in text and "2200: Timeout" in text
    assert "Lookup" not in text


def test_poll_skips_already_seen_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(adf, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(adf, "fetch_failed_runs", lambda h: [RUN])
    monkeypatch.setattr(adf, "fetch_activity_runs", lambda r: ACTS)
    calls = []
    monkeypatch.setattr(adf, "triage_run", lambda run, acts: calls.append(run["runId"]))
    adf.poll()
    adf.poll()
    assert calls == ["r1"]


def _webhook(monkeypatch, secret="s3"):
    monkeypatch.setenv("ADF_WEBHOOK_SECRET", "s3")
    monkeypatch.setattr(adf, "fetch_activity_runs",
                        lambda r: (_ for _ in ()).throw(adf.AdfError("unset")))
    monkeypatch.setattr(adf, "triage_run", lambda run, acts: Triage(
        failures=[Failure(failure_type="t", what_broke="w", evidence="e", next_step="n")],
        notes="ok"))
    return TestClient(local_server.app).post(
        "/api/adf/webhook",
        json={"pipelineName": "P", "runId": "r9", "message": "boom"},
        headers={"X-ADF-Secret": secret})


def test_webhook_triages_with_valid_secret(monkeypatch):
    r = _webhook(monkeypatch)
    assert r.status_code == 200 and r.json() == {"failures": 1}


def test_webhook_rejects_bad_secret(monkeypatch):
    assert _webhook(monkeypatch, secret="nope").status_code == 401


def test_azure_app_webhook_and_poll(monkeypatch):
    from azure_app import main
    monkeypatch.setenv("ADF_WEBHOOK_SECRET", "s3")
    monkeypatch.setattr(adf, "triage_webhook", lambda *a: Triage(
        failures=[Failure(failure_type="t", what_broke="w", evidence="e", next_step="n")],
        notes="ok"))
    monkeypatch.setattr(adf, "poll", lambda hours: ["r1", "r2"])
    c = TestClient(main.app)
    h = {"X-ADF-Secret": "s3"}
    hook = c.post("/api/adf/webhook", json={"runId": "r1"}, headers=h)
    assert hook.json() == {"failures": 1}
    assert c.post("/api/adf/webhook", json={"runId": "r1"}).status_code == 401
    assert c.post("/api/adf/poll", headers=h).json() == {"triaged": 2, "run_ids": ["r1", "r2"]}
    assert c.post("/api/adf/poll").status_code == 401


def test_managed_identity_token_used_when_present(monkeypatch):
    monkeypatch.delenv("ADF_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://x")
    monkeypatch.setenv("IDENTITY_HEADER", "h")
    monkeypatch.setattr(adf, "_managed_identity_token", lambda: "mi-token")
    assert adf._token() == "mi-token"
