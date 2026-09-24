"""Best-effort alerts for urgent or recurring findings.

Configured entirely by environment variables; with none set this is a no-op.
  TRIAGE_WEBHOOK_URL   Slack- or Teams-compatible incoming webhook (JSON {"text": ...})
  TRIAGE_SMTP_HOST     with TRIAGE_SMTP_FROM, optionally TRIAGE_SMTP_PORT (587),
                       TRIAGE_SMTP_USER / TRIAGE_SMTP_PASSWORD: also email the owning
                       team's on-call address
Never raises: a failed alert must not fail the triage.
"""

import json
import os
import smtplib
import sys
import urllib.request
from email.message import EmailMessage

RECURRENCE_THRESHOLD = 3


def is_urgent(finding: dict) -> bool:
    return finding.get("priority") in ("P0", "P1") or \
        int(finding.get("occurrences") or 0) >= RECURRENCE_THRESHOLD


def format_message(log_file: str, findings: list[dict]) -> str:
    lines = [f"Pipeline triage: {len(findings)} finding(s) need attention in {log_file}"]
    for f in findings:
        seen = int(f.get("occurrences") or 1)
        repeat = f" - seen {seen} times" if seen > 1 else ""
        lines.append(f"- [{f['priority']}/{f['severity']}] {f['title']} "
                     f"-> {f.get('team_name') or 'unassigned'}{repeat}\n  {f['root_cause']}")
    return "\n".join(lines)


def _post_webhook(url: str, text: str) -> None:
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).close()  # noqa: S310 - operator-set URL


def _send_email(host: str, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["TRIAGE_SMTP_FROM"], to, subject
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("TRIAGE_SMTP_PORT", "587")), timeout=10) as smtp:
        smtp.starttls()
        if os.environ.get("TRIAGE_SMTP_USER"):
            smtp.login(os.environ["TRIAGE_SMTP_USER"], os.environ.get("TRIAGE_SMTP_PASSWORD", ""))
        smtp.send_message(msg)


def notify_findings(log_file: str, findings: list[dict]) -> None:
    urgent = [f for f in findings if is_urgent(f)]
    if not urgent:
        return
    text = format_message(log_file, urgent)
    webhook, smtp_host = os.environ.get("TRIAGE_WEBHOOK_URL"), os.environ.get("TRIAGE_SMTP_HOST")
    if webhook:
        try:
            _post_webhook(webhook, text)
        except Exception as e:  # noqa: BLE001
            print(f"note: webhook notification failed: {e}", file=sys.stderr)
    if smtp_host and os.environ.get("TRIAGE_SMTP_FROM"):
        for to in sorted({f["oncall_email"] for f in urgent if f.get("oncall_email")}):
            mine = [f for f in urgent if f.get("oncall_email") == to]
            try:
                _send_email(smtp_host, to, f"[triage] {len(mine)} urgent finding(s)",
                            format_message(log_file, mine))
            except Exception as e:  # noqa: BLE001
                print(f"note: email to {to} failed: {e}", file=sys.stderr)
