"""Severity/confidence, repeat detection, alerts, decoding, truncation, auth and rate limits."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402
import Triage  # noqa: E402
import triage_db  # noqa: E402
from azure_app import main as azure  # noqa: E402


def test_failure_defaults_and_clamping():
    f = Triage.Failure(failure_type="t", what_broke="w", evidence="e", next_step="n",
                       severity="URGENT", confidence=7)
    assert (f.severity, f.confidence, f.suggested_fix) == ("medium", 1.0, "")
    g = Triage.Failure(failure_type="t", what_broke="w", evidence="e", next_step="n",
                       severity="Critical", confidence="oops")
    assert (g.severity, g.confidence) == ("critical", 0.5)


def test_decode_log_bytes_handles_utf16_utf8_and_latin1():
    assert Triage.decode_log_bytes("héllo\n".encode("utf-16")) == ("héllo\n", False)
    assert Triage.decode_log_bytes("héllo".encode("utf-8")) == ("héllo", False)
    assert Triage.decode_log_bytes("héllo".encode("latin-1")) == ("héllo", True)


def test_truncation_keeps_error_lines_from_the_middle():
    filler = "ok line\n" * (Triage.MAX_LOG_CHARS // 4)
    text = "START\n" + filler + "FATAL: disk failed\n" + filler + "END\n"
    out, truncated = Triage._truncate_log_text(text)
    assert truncated and "FATAL: disk failed" in out
    assert out.startswith("START") and out.endswith("END\n")
    assert len(out) < len(text)


def test_priority_follows_severity_but_blocker_is_at_least_p1():
    assert triage_db.priority_for(0, "critical") == ("P0", "critical")
    assert triage_db.priority_for(0, "low") == ("P1", "high")
    assert triage_db.priority_for(1, "low") == ("P3", "low")
    assert triage_db.priority_for(1) == ("P2", "medium")


def test_signature_ignores_numbers_and_ids_but_not_category():
    a = triage_db.failure_signature("Task 42 timed out on shard 7", "infra")
    b = triage_db.failure_signature("Task 99 timed out on shard 3", "infra")
    assert a == b
    assert a != triage_db.failure_signature("Task 42 timed out on shard 7", "other")


def test_finding_markdown_labels_ai_suggestion():
    md = triage_db.finding_markdown({
        "id": 3, "title": "Disk full", "priority": "P1", "severity": "high", "status": "new",
        "root_cause": "no space", "evidence": "ENOSPC", "suggested_fix": "clean tmp",
        "proposed_fix": "rm -rf /tmp/spill", "occurrences": 4, "confidence": 0.8,
    })
    assert "Seen: 4 time(s)" in md and "Confidence: 80%" in md
    assert "AI suggestion - review before running" in md and "rm -rf /tmp/spill" in md


def _finding(**kw):
    base = {"priority": "P2", "severity": "medium", "title": "T", "root_cause": "r",
            "occurrences": 1, "team_name": "Platform", "oncall_email": "oncall@example.com"}
    return {**base, **kw}


def test_is_urgent_for_p1_or_repeats_only():
    assert notify.is_urgent(_finding(priority="P1"))
    assert notify.is_urgent(_finding(occurrences=3))
    assert not notify.is_urgent(_finding())


def test_notify_posts_urgent_findings_to_webhook(monkeypatch):
    sent = []
    monkeypatch.setenv("TRIAGE_WEBHOOK_URL", "https://hooks.example/x")
    monkeypatch.delenv("TRIAGE_SMTP_HOST", raising=False)
    monkeypatch.setattr(notify, "_post_webhook", lambda url, text: sent.append((url, text)))
    notify.notify_findings("nightly.log", [_finding(priority="P1"), _finding(title="minor")])
    assert len(sent) == 1 and "T" in sent[0][1] and "minor" not in sent[0][1]


def test_notify_never_raises_and_skips_when_nothing_urgent(monkeypatch):
    monkeypatch.setenv("TRIAGE_WEBHOOK_URL", "https://hooks.example/x")

    def boom(url, text):
        raise OSError("down")

    monkeypatch.setattr(notify, "_post_webhook", boom)
    notify.notify_findings("a.log", [_finding(priority="P1")])   # must not raise
    monkeypatch.setattr(notify, "_post_webhook", lambda *a: (_ for _ in ()).throw(AssertionError))
    notify.notify_findings("a.log", [_finding()])                # nothing urgent: no call


def test_identity_ingest_token_session_token_and_access_code(monkeypatch):
    monkeypatch.setenv("TRIAGE_ACCESS_CODE", "code1")
    monkeypatch.setenv("TRIAGE_INGEST_TOKEN", "ci-secret")
    monkeypatch.setenv("TRIAGE_SECRET_KEY", "k")
    assert azure._identity("", "Bearer ci-secret") == "ci"
    assert azure._identity("code1", "") == "code"
    assert azure._identity("nope", "") is None
    assert azure._identity("", "Bearer " + triage_db.make_token(7)) == "user:7"
    assert azure._identity("", "Bearer garbage") is None


def test_identity_open_only_when_nothing_configured(monkeypatch):
    for k in ("TRIAGE_ACCESS_CODE", "TRIAGE_INGEST_TOKEN", "TRIAGE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert azure._identity("", "") == "open"
    monkeypatch.setenv("TRIAGE_INGEST_TOKEN", "x")
    assert azure._identity("", "") is None


def test_rate_limit_is_per_caller_and_slides(monkeypatch):
    monkeypatch.setattr(azure, "RATE_LIMIT_PER_MINUTE", 2)
    azure._hits.clear()
    assert not azure._rate_limited("a", now=0) and not azure._rate_limited("a", now=1)
    assert azure._rate_limited("a", now=2)
    assert not azure._rate_limited("b", now=2)
    assert not azure._rate_limited("a", now=100)
