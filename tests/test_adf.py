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
