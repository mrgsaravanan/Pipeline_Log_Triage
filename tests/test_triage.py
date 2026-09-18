"""Unit tests for Triage.py.

These tests never invoke the real `claude` CLI: subprocess.run is
monkeypatched everywhere it would be called, so the suite runs offline,
deterministically, and for free.
"""

import base64
import json
import subprocess
import sys
from pathlib import Path

import anthropic
import httpx2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import Triage
from Triage import Failure, main, print_report
from Triage import Triage as TriageModel


@pytest.fixture(autouse=True)
def _isolated_history_file(monkeypatch, tmp_path):
    """Every test gets its own history file so runs don't pollute the repo
    or leak state between tests. Tests of the failure path override this."""
    monkeypatch.setattr(Triage, "HISTORY_FILE", str(tmp_path / "history.jsonl"))


@pytest.fixture(autouse=True)
def _default_to_cli_backend(monkeypatch):
    """A developer's (or Vercel's) env must not flip the backend under test."""
    monkeypatch.delenv("TRIAGE_BACKEND", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)


def _cli_envelope(*, result: str, is_error: bool = False) -> str:
    return json.dumps({"type": "result", "is_error": is_error, "result": result})


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# --- _read_log_file: multiple log encodings ----------------------------------


def test_read_log_file_reads_utf8_normally(tmp_path):
    log_file = tmp_path / "utf8.log"
    log_file.write_text("café — 2026-09-11 ERROR extract failed", encoding="utf-8")

    text, used_fallback = Triage._read_log_file(str(log_file))

    assert used_fallback is False
    assert "café" in text


def test_read_log_file_falls_back_to_latin1_on_invalid_utf8(tmp_path):
    log_file = tmp_path / "legacy.log"
    log_file.write_bytes("café ERROR pull_client_file failed".encode("latin-1"))

    text, used_fallback = Triage._read_log_file(str(log_file))

    assert used_fallback is True
    assert "café" in text


# --- _truncate_log_text: oversized logs --------------------------------------


def test_truncate_log_text_leaves_short_text_unchanged():
    short_text = "small log, well under the cap"

    text, was_truncated = Triage._truncate_log_text(short_text)

    assert was_truncated is False
    assert text == short_text


def test_truncate_log_text_keeps_head_and_tail_of_oversized_text():
    head_marker = "HEAD_MARKER_START"
    tail_marker = "TAIL_MARKER_END"
    middle_filler = "x" * (Triage.MAX_LOG_CHARS + 1000)
    huge_text = head_marker + middle_filler + tail_marker

    text, was_truncated = Triage._truncate_log_text(huge_text)

    assert was_truncated is True
    assert text.startswith(head_marker)
    assert text.endswith(tail_marker)
    assert "omitted" in text
    assert len(text) < len(huge_text)


# --- main(): encoding/truncation notes surfaced on stderr --------------------


def test_main_notes_encoding_fallback_but_still_succeeds(monkeypatch, capsys, tmp_path):
    log_file = tmp_path / "legacy.log"
    log_file.write_bytes("café ERROR something broke".encode("latin-1"))
    monkeypatch.setattr(sys, "argv", ["Triage.py", str(log_file)])

    payload = json.dumps({"failures": [], "notes": "nothing broke"})
    monkeypatch.setattr(
        Triage.subprocess, "run", lambda *a, **k: _completed(stdout=_cli_envelope(result=payload))
    )

    assert main() == 0
    err = capsys.readouterr().err
    assert "not valid UTF-8" in err


def test_main_notes_truncation_but_still_succeeds(monkeypatch, capsys, tmp_path):
    log_file = tmp_path / "huge.log"
    log_file.write_text("x" * (Triage.MAX_LOG_CHARS + 1000))
    monkeypatch.setattr(sys, "argv", ["Triage.py", str(log_file)])

    payload = json.dumps({"failures": [], "notes": "nothing broke"})
    monkeypatch.setattr(
        Triage.subprocess, "run", lambda *a, **k: _completed(stdout=_cli_envelope(result=payload))
    )

    assert main() == 0
    err = capsys.readouterr().err
    assert "large" in err


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


def _write_log(tmp_path, monkeypatch):
    log_file = tmp_path / "pipeline.log"
    log_file.write_text("2024-01-01 ERROR something broke")
    monkeypatch.setattr(sys, "argv", ["Triage.py", str(log_file)])
    return log_file


def test_main_prints_formatted_report_on_success(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)

    payload = json.dumps(
        {
            "failures": [
                {
                    "failure_type": "upstream schema drift",
                    "what_broke": "A column was dropped upstream.",
                    "evidence": "line 7",
                    "next_step": "Restore the column or update the consumer.",
                }
            ],
            "notes": "",
        }
    )
    fake_run = lambda *a, **k: _completed(stdout=_cli_envelope(result=payload))  # noqa: E731
    monkeypatch.setattr(Triage.subprocess, "run", fake_run)

    assert main() == 0
    out = capsys.readouterr().out
    assert "upstream schema drift" in out
    assert "What broke: A column was dropped upstream." in out


def test_main_strips_markdown_code_fence_from_result(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)

    payload = json.dumps({"failures": [], "notes": "nothing broke"})
    fenced = f"```json\n{payload}\n```"
    monkeypatch.setattr(
        Triage.subprocess, "run", lambda *a, **k: _completed(stdout=_cli_envelope(result=fenced))
    )

    assert main() == 0
    assert "Notes: nothing broke" in capsys.readouterr().out


# --- persistence: history file ------------------------------------------------


def test_append_history_writes_one_jsonl_record(monkeypatch, tmp_path):
    history_file = tmp_path / "history.jsonl"
    monkeypatch.setattr(Triage, "HISTORY_FILE", str(history_file))

    triage = TriageModel(
        failures=[
            Failure(failure_type="X", what_broke="y", evidence="z", next_step="w")
        ],
        notes="",
    )
    Triage._append_history("some.log", triage)

    lines = history_file.read_text().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["log_path"] == "some.log"
    assert record["model"] == Triage.MODEL
    assert record["triage"]["failures"][0]["failure_type"] == "X"
    # timestamp should be a real, parseable ISO-8601 datetime
    from datetime import datetime

    datetime.fromisoformat(record["timestamp"])


def test_main_appends_history_record_on_success(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)
    history_file = Path(Triage.HISTORY_FILE)

    payload = json.dumps(
        {
            "failures": [
                {
                    "failure_type": "upstream schema drift",
                    "what_broke": "A column was dropped upstream.",
                    "evidence": "line 7",
                    "next_step": "Restore the column.",
                }
            ],
            "notes": "",
        }
    )
    monkeypatch.setattr(
        Triage.subprocess, "run", lambda *a, **k: _completed(stdout=_cli_envelope(result=payload))
    )

    assert main() == 0
    record = json.loads(history_file.read_text().splitlines()[0])
    assert record["triage"]["failures"][0]["failure_type"] == "upstream schema drift"


def test_main_still_succeeds_when_history_write_fails(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)
    # Point HISTORY_FILE at a directory: opening it for append raises OSError
    # (IsADirectoryError), independent of filesystem permissions.
    monkeypatch.setattr(Triage, "HISTORY_FILE", str(tmp_path))

    payload = json.dumps({"failures": [], "notes": "nothing broke"})
    monkeypatch.setattr(
        Triage.subprocess, "run", lambda *a, **k: _completed(stdout=_cli_envelope(result=payload))
    )

    assert main() == 0
    captured = capsys.readouterr()
    assert "Notes: nothing broke" in captured.out
    assert "could not write to history file" in captured.err


# --- main(): claude CLI failure handling -------------------------------------


def test_main_reports_missing_cli(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)

    def raise_not_found(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(Triage.subprocess, "run", raise_not_found)

    assert main() == 1
    assert "claude CLI not found" in capsys.readouterr().err


def test_main_reports_timeout(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=180)

    monkeypatch.setattr(Triage.subprocess, "run", raise_timeout)

    assert main() == 1
    assert "timed out" in capsys.readouterr().err


def test_main_reports_nonzero_exit(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)

    monkeypatch.setattr(
        Triage.subprocess,
        "run",
        lambda *a, **k: _completed(returncode=1, stderr="not logged in"),
    )

    assert main() == 1
    assert "not logged in" in capsys.readouterr().err


def test_main_reports_is_error_envelope(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)

    monkeypatch.setattr(
        Triage.subprocess,
        "run",
        lambda *a, **k: _completed(stdout=_cli_envelope(result="rate limited", is_error=True)),
    )

    assert main() == 1
    assert "rate limited" in capsys.readouterr().err


def test_main_reports_malformed_outer_json(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)

    monkeypatch.setattr(Triage.subprocess, "run", lambda *a, **k: _completed(stdout="not json"))

    assert main() == 1
    assert "could not parse claude CLI output as JSON" in capsys.readouterr().err


def test_main_reports_schema_mismatch(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)

    bad_payload = json.dumps({"unexpected": "shape"})
    monkeypatch.setattr(
        Triage.subprocess,
        "run",
        lambda *a, **k: _completed(stdout=_cli_envelope(result=bad_payload)),
    )

    assert main() == 1
    assert "did not match the expected schema" in capsys.readouterr().err


# --- backend selection: local CLI vs hosted API -------------------------------


def test_backend_defaults_to_cli():
    assert Triage._triage_backend() == "cli"


def test_backend_is_api_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    assert Triage._triage_backend() == "api"


def test_explicit_backend_overrides_vercel_detection(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("TRIAGE_BACKEND", "cli")
    assert Triage._triage_backend() == "cli"


def test_unknown_backend_is_a_clean_error(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)
    monkeypatch.setenv("TRIAGE_BACKEND", "bogus")

    assert main() == 1
    assert "unknown TRIAGE_BACKEND" in capsys.readouterr().err


# --- API backend (hosted) ------------------------------------------------------

API_PAYLOAD = json.dumps(
    {
        "failures": [
            {
                "failure_type": "S3 permission denied",
                "what_broke": "GetObject was denied.",
                "evidence": "AccessDenied",
                "next_step": "Fix the IAM policy.",
            }
        ],
        "notes": "",
    }
)


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_FakeBlock(text)]
        self.stop_reason = stop_reason


class _FakeClient:
    """Stands in for anthropic.Anthropic; records the create() call."""

    def __init__(self, response=None, error=None):
        self._response, self._error, self.calls = response, error, []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


def _use_api(monkeypatch, client):
    monkeypatch.setenv("TRIAGE_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: client)


def test_api_backend_returns_parsed_triage_and_sends_prompt(monkeypatch):
    client = _FakeClient(response=_FakeResponse(API_PAYLOAD))
    _use_api(monkeypatch, client)

    triage = Triage.run_triage("x.log", "2024 ERROR boom")

    assert triage.failures[0].failure_type == "S3 permission denied"
    call = client.calls[0]
    assert call["model"] == Triage.MODEL
    assert call["max_tokens"] == Triage.API_MAX_TOKENS
    assert Triage.SYSTEM_PROMPT in call["system"]
    assert "2024 ERROR boom" in call["messages"][0]["content"]


def test_api_backend_strips_code_fence(monkeypatch):
    _use_api(monkeypatch, _FakeClient(response=_FakeResponse(f"```json\n{API_PAYLOAD}\n```")))

    assert Triage.run_triage("x.log", "log").failures[0].what_broke == "GetObject was denied."


def test_api_backend_requires_api_key(monkeypatch):
    monkeypatch.setenv("TRIAGE_BACKEND", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(Triage.ClaudeApiError, match="ANTHROPIC_API_KEY is not set"):
        Triage.run_triage("x.log", "log")


def test_api_backend_rejects_truncated_response(monkeypatch):
    truncated = _FakeResponse(API_PAYLOAD[:40], stop_reason="max_tokens")
    _use_api(monkeypatch, _FakeClient(response=truncated))

    with pytest.raises(Triage.ClaudeApiError, match="truncated"):
        Triage.run_triage("x.log", "log")


def test_api_backend_rejects_schema_mismatch(monkeypatch):
    _use_api(monkeypatch, _FakeClient(response=_FakeResponse('{"unexpected": "shape"}')))

    with pytest.raises(Triage.ClaudeCliError, match="did not match the expected schema"):
        Triage.run_triage("x.log", "log")


@pytest.mark.parametrize(
    "error, expected",
    [
        (
            anthropic.AuthenticationError(
                "bad key",
                response=httpx2.Response(401, request=httpx2.Request("POST", "https://x")),
                body=None,
            ),
            "rejected the API key",
        ),
        (
            anthropic.APIStatusError(
                "overloaded",
                response=httpx2.Response(529, request=httpx2.Request("POST", "https://x")),
                body=None,
            ),
            "Anthropic API error (529)",
        ),
        (
            anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
            "network error",
        ),
    ],
)
def test_api_backend_maps_sdk_errors_to_clean_messages(
    monkeypatch, capsys, tmp_path, error, expected
):
    _write_log(tmp_path, monkeypatch)
    _use_api(monkeypatch, _FakeClient(error=error))

    assert main() == 1
    err = capsys.readouterr().err
    assert err.startswith("API error:")
    assert expected in err


def test_main_prints_report_via_api_backend(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)
    _use_api(monkeypatch, _FakeClient(response=_FakeResponse(API_PAYLOAD)))

    assert main() == 0
    assert "S3 permission denied" in capsys.readouterr().out


# --- web app: optional access code ---------------------------------------------


def test_web_access_code_not_required_when_unset(monkeypatch):
    from azure_app import main as web

    monkeypatch.delenv("TRIAGE_ACCESS_CODE", raising=False)
    assert web._access_ok("") is True
    assert "access_code" not in web._form_html()


def test_web_access_code_enforced_when_set(monkeypatch):
    from azure_app import main as web

    monkeypatch.setenv("TRIAGE_ACCESS_CODE", "s3cret")
    assert web._access_ok("s3cret") is True
    assert web._access_ok("wrong") is False
    assert web._access_ok("") is False
    assert 'name="access_code"' in web._form_html()


# --- image (screenshot) logs ----------------------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"not-a-real-image-but-has-the-magic-bytes"


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"GIF89arest", "image/gif"),
        (b"GIF87arest", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", None),  # RIFF but not WebP
        (b"2024-01-01 ERROR plain text log", None),
        (b"%PDF-1.7 ...", None),
        (b"", None),
        (b"\x89PN", None),  # truncated header
    ],
)
def test_detect_image_media_type(data, expected):
    assert Triage.detect_image_media_type(data) == expected


def test_api_image_request_carries_image_block_and_triage_prompt(monkeypatch):
    client = _FakeClient(response=_FakeResponse(API_PAYLOAD))
    _use_api(monkeypatch, client)

    triage = Triage.run_triage_image("shot.png", PNG_BYTES, "image/png")

    assert triage.failures[0].failure_type == "S3 permission denied"
    call = client.calls[0]
    assert Triage.SYSTEM_PROMPT in call["system"]
    image_block, text_block = call["messages"][0]["content"]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"
    assert base64.b64decode(image_block["source"]["data"]) == PNG_BYTES
    assert text_block["type"] == "text"
    assert "shot.png" in text_block["text"]
    assert "cut off" in text_block["text"]  # the don't-guess-unreadable-text instruction


def test_image_triage_refuses_cli_backend_with_clear_message():
    # autouse fixture leaves the default (cli) backend in place
    with pytest.raises(Triage.ClaudeCliError, match="image logs need the API backend"):
        Triage.run_triage_image("shot.png", PNG_BYTES, "image/png")


def test_image_triage_shares_api_error_handling(monkeypatch):
    monkeypatch.setenv("TRIAGE_BACKEND", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(Triage.ClaudeApiError, match="ANTHROPIC_API_KEY is not set"):
        Triage.run_triage_image("shot.png", PNG_BYTES, "image/png")


def test_main_triages_image_file_on_api_backend(monkeypatch, capsys, tmp_path):
    image_file = tmp_path / "failure.png"
    image_file.write_bytes(PNG_BYTES)
    monkeypatch.setattr(sys, "argv", ["Triage.py", str(image_file)])
    client = _FakeClient(response=_FakeResponse(API_PAYLOAD))
    _use_api(monkeypatch, client)

    assert main() == 0
    assert "S3 permission denied" in capsys.readouterr().out
    assert client.calls[0]["messages"][0]["content"][0]["type"] == "image"


def test_main_image_on_cli_backend_fails_cleanly(monkeypatch, capsys, tmp_path):
    image_file = tmp_path / "failure.png"
    image_file.write_bytes(PNG_BYTES)
    monkeypatch.setattr(sys, "argv", ["Triage.py", str(image_file)])

    assert main() == 1
    assert "image logs need the API backend" in capsys.readouterr().err


def test_main_rejects_oversized_image(monkeypatch, capsys, tmp_path):
    image_file = tmp_path / "huge.png"
    image_file.write_bytes(PNG_BYTES + b"x" * Triage.MAX_IMAGE_BYTES)
    monkeypatch.setattr(sys, "argv", ["Triage.py", str(image_file)])
    client = _FakeClient(response=_FakeResponse(API_PAYLOAD))
    _use_api(monkeypatch, client)

    assert main() == 1
    assert "larger than" in capsys.readouterr().err
    assert client.calls == []  # rejected before any (billed) API call


def test_text_log_still_uses_text_path_not_image_path(monkeypatch, capsys, tmp_path):
    _write_log(tmp_path, monkeypatch)
    client = _FakeClient(response=_FakeResponse(API_PAYLOAD))
    _use_api(monkeypatch, client)

    assert main() == 0
    assert isinstance(client.calls[0]["messages"][0]["content"], str)


# --- web app: uploads -----------------------------------------------------------


def _upload(name: str, data: bytes):
    import io

    from starlette.datastructures import UploadFile

    return UploadFile(file=io.BytesIO(data), filename=name)


def _post_triage(**kwargs):
    import asyncio

    from azure_app import main as web

    return asyncio.run(web.triage(**kwargs))


def test_web_image_upload_is_triaged_via_api(monkeypatch):
    client = _FakeClient(response=_FakeResponse(API_PAYLOAD))
    _use_api(monkeypatch, client)

    html = _post_triage(log_text="", log_file=_upload("shot.png", PNG_BYTES), access_code="")

    assert "S3 permission denied" in html
    assert client.calls[0]["messages"][0]["content"][0]["type"] == "image"


def test_web_image_on_cli_backend_shows_clean_error(monkeypatch):
    html = _post_triage(log_text="", log_file=_upload("shot.png", PNG_BYTES), access_code="")

    assert "Triage failed: image logs need the API backend" in html


def test_web_rejects_oversized_image_before_calling_api(monkeypatch):
    client = _FakeClient(response=_FakeResponse(API_PAYLOAD))
    _use_api(monkeypatch, client)
    big = PNG_BYTES + b"x" * Triage.MAX_IMAGE_BYTES

    html = _post_triage(log_text="", log_file=_upload("big.png", big), access_code="")

    assert "Image is larger than" in html
    assert client.calls == []


def test_web_rejects_non_image_binary_file(monkeypatch):
    client = _FakeClient(response=_FakeResponse(API_PAYLOAD))
    _use_api(monkeypatch, client)

    html = _post_triage(
        log_text="", log_file=_upload("report.pdf", b"%PDF-1.7\x00\x01\x02binary"), access_code=""
    )

    assert "looks like a binary file" in html
    assert client.calls == []


def test_web_text_upload_still_works(monkeypatch):
    client = _FakeClient(response=_FakeResponse(API_PAYLOAD))
    _use_api(monkeypatch, client)

    html = _post_triage(
        log_text="", log_file=_upload("app.log", b"2024 ERROR boom\n"), access_code=""
    )

    assert "S3 permission denied" in html
    assert isinstance(client.calls[0]["messages"][0]["content"], str)


def test_web_image_upload_still_respects_access_code(monkeypatch):
    monkeypatch.setenv("TRIAGE_ACCESS_CODE", "s3cret")
    client = _FakeClient(response=_FakeResponse(API_PAYLOAD))
    _use_api(monkeypatch, client)

    html = _post_triage(log_text="", log_file=_upload("shot.png", PNG_BYTES), access_code="nope")

    assert "Incorrect access code" in html
    assert client.calls == []
