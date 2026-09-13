"""Unit tests for Triage.py.

These tests never call the real Anthropic API: anthropic.Anthropic() is
monkeypatched everywhere a client would be constructed, so the suite runs
offline, deterministically, and for free.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import Triage
from Triage import Failure, main, print_report
from Triage import Triage as TriageModel


def _fake_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _fake_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_fake_request())


# --- print_report -----------------------------------------------------------


def test_print_report_single_failure_omits_count_and_notes(capsys):
    triage = TriageModel(
        failures=[
            Failure(
                failure_type="S3 permission denied",
                what_broke="The writer role lost s3:PutObject on the target bucket.",
                evidence="AccessDenied at line 42",
                next_step="Re-attach the s3:PutObject policy to the writer role.",
            )
        ],
        notes="",
    )

    print_report(triage)
    out = capsys.readouterr().out

    assert "distinct failures found" not in out
    assert "S3 permission denied" in out
    assert "What broke: The writer role lost s3:PutObject on the target bucket." in out
    assert "Notes:" not in out


def test_print_report_multiple_failures_numbers_and_shows_notes(capsys):
    triage = TriageModel(
        failures=[
            Failure(failure_type="A", what_broke="a", evidence="a", next_step="a"),
            Failure(failure_type="B", what_broke="b", evidence="b", next_step="b"),
        ],
        notes="Two unrelated root causes.",
    )

    print_report(triage)
    out = capsys.readouterr().out

    assert "2 distinct failures found." in out
    assert "1. A" in out
    assert "2. B" in out
    assert "Notes: Two unrelated root causes." in out


# --- main(): argument / file validation --------------------------------------


def test_main_requires_exactly_one_argument(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["Triage.py"])

    assert main() == 1
    assert "usage:" in capsys.readouterr().err


def test_main_reports_unreadable_file(monkeypatch, capsys, tmp_path):
    missing = tmp_path / "does-not-exist.log"
    monkeypatch.setattr(sys, "argv", ["Triage.py", str(missing)])

    assert main() == 1
    assert "could not read" in capsys.readouterr().err


def test_main_rejects_empty_file(monkeypatch, capsys, tmp_path):
    empty_log = tmp_path / "empty.log"
    empty_log.write_text("")
    monkeypatch.setattr(sys, "argv", ["Triage.py", str(empty_log)])

    assert main() == 1
    assert "nothing to triage" in capsys.readouterr().err


# --- main(): success path, wired to print_report -----------------------------


def test_main_prints_formatted_report_on_success(monkeypatch, capsys, tmp_path):
    log_file = tmp_path / "pipeline.log"
    log_file.write_text("2024-01-01 ERROR something broke")
    monkeypatch.setattr(sys, "argv", ["Triage.py", str(log_file)])

    fake_triage = TriageModel(
        failures=[
            Failure(
                failure_type="upstream schema drift",
                what_broke="A column was dropped upstream.",
                evidence="line 7",
                next_step="Restore the column or update the consumer.",
            )
        ],
        notes="",
    )
    fake_response = MagicMock()
    fake_response.parsed_output = fake_triage
    fake_response.content = []

    mock_client = MagicMock()
    mock_client.messages.parse.return_value = fake_response
    monkeypatch.setattr(Triage.anthropic, "Anthropic", lambda: mock_client)

    assert main() == 0
    out = capsys.readouterr().out
    assert "upstream schema drift" in out
    assert "What broke: A column was dropped upstream." in out
    mock_client.messages.parse.assert_called_once()


# --- main(): API error handling ----------------------------------------------


@pytest.mark.parametrize(
    "exc, expected_substr",
    [
        (
            anthropic.AuthenticationError("bad key", response=_fake_response(401), body=None),
            "auth failed",
        ),
        (
            anthropic.APIStatusError("server error", response=_fake_response(500), body=None),
            "API error (500)",
        ),
        (
            anthropic.APIConnectionError(request=_fake_request()),
            "network error",
        ),
    ],
)
def test_main_reports_api_errors_on_stderr(monkeypatch, capsys, tmp_path, exc, expected_substr):
    log_file = tmp_path / "pipeline.log"
    log_file.write_text("2024-01-01 ERROR something broke")
    monkeypatch.setattr(sys, "argv", ["Triage.py", str(log_file)])

    mock_client = MagicMock()
    mock_client.messages.parse.side_effect = exc
    monkeypatch.setattr(Triage.anthropic, "Anthropic", lambda: mock_client)

    assert main() == 1
    assert expected_substr in capsys.readouterr().err
