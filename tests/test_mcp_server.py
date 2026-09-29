"""Unit tests for mcp_server.py.

Skipped entirely if the optional `mcp` package (requirements-mcp.txt) isn't
installed - it is never required for the CLI, tests, or either hosted app.
Like test_triage.py, the CLI path here never invokes the real `claude` CLI.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("mcp")

import mcp_server  # noqa: E402
import Triage  # noqa: E402


def _cli_envelope(*, result: str, is_error: bool = False) -> str:
    return json.dumps({"type": "result", "is_error": is_error, "result": result})


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    import subprocess
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


API_PAYLOAD = json.dumps({
    "failures": [{
        "failure_type": "S3 permission denied", "what_broke": "w", "evidence": "e",
        "next_step": "n", "severity": "high", "confidence": 0.9, "suggested_fix": "",
        "category": "permissions",
    }],
    "notes": "",
})


@pytest.fixture(autouse=True)
def _isolated_history_file(monkeypatch, tmp_path):
    monkeypatch.setattr(Triage, "HISTORY_FILE", str(tmp_path / "history.jsonl"))


@pytest.fixture(autouse=True)
def _no_database_url(monkeypatch):
    """The DB-backed tools are exercised only for their no-DB error path here -
    a real connection is covered by tests/test_triage_db.py."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_forces_cli_backend_on_import():
    # mcp_server.py must never fall through to the billed API backend.
    import os
    assert os.environ["TRIAGE_BACKEND"] == "cli"


def test_all_tools_are_registered():
    import asyncio

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert names == {"triage_log", "list_findings", "get_finding", "get_trends"}


def test_triage_log_rejects_empty_text():
    assert mcp_server.triage_log("") == {"error": "log_text is empty"}
    assert mcp_server.triage_log("   \n") == {"error": "log_text is empty"}


def test_triage_log_runs_via_cli_and_appends_history(monkeypatch, tmp_path):
    history = tmp_path / "history.jsonl"
    monkeypatch.setattr(Triage, "HISTORY_FILE", str(history))

    def fake_run(cmd, **kwargs):
        return _completed(stdout=_cli_envelope(result=API_PAYLOAD))

    monkeypatch.setattr(Triage.subprocess, "run", fake_run)

    report = mcp_server.triage_log("2024 ERROR AccessDenied", name="x.log")

    assert report["failures"][0]["failure_type"] == "S3 permission denied"
    assert history.read_text().strip()  # _append_history wrote a record
    assert "x.log" in history.read_text()


def test_triage_log_missing_cli_is_a_clear_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("no such file: claude")

    monkeypatch.setattr(Triage.subprocess, "run", fake_run)

    report = mcp_server.triage_log("2024 ERROR boom")
    assert "claude login" in report["error"]


def test_triage_log_cli_timeout_is_a_clear_error(monkeypatch):
    import subprocess

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(Triage.subprocess, "run", fake_run)

    report = mcp_server.triage_log("2024 ERROR boom")
    assert "timed out" in report["error"]


def test_findings_tools_error_clearly_without_database_url():
    assert "DATABASE_URL" in mcp_server.list_findings()["error"]
    assert "DATABASE_URL" in mcp_server.get_finding(1)["error"]
    assert "DATABASE_URL" in mcp_server.get_trends()["error"]
