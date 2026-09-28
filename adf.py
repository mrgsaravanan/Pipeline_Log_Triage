"""Azure Data Factory integration: triage failed pipeline runs automatically.

Two entry points feed the same path (ADF run -> log text -> Triage -> history/DB):

  python adf.py --hours 24     poll ADF for failed runs and triage each new one
  POST /api/adf/webhook        called by an ADF Web activity on the pipeline's
                               Failure path (see local_server.py)

Configuration (environment variables):
  ADF_SUBSCRIPTION_ID, ADF_RESOURCE_GROUP, ADF_FACTORY_NAME   the factory to read
  ADF_ACCESS_TOKEN     optional bearer token for management.azure.com; when unset
                       the token comes from `az account get-access-token`
                       (run `az login` once - no secrets stored in this repo)
  ADF_WEBHOOK_SECRET   shared secret the webhook requires in X-ADF-Secret

Runs already triaged are remembered in .adf_seen.json (local, gitignored) so
re-polling never spends the subscription twice on the same run.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_VERSION = "2018-06-01"
SEEN_FILE = Path(__file__).resolve().parent / ".adf_seen.json"
MGMT = "https://management.azure.com"


class AdfError(Exception):
    pass


def _factory_base() -> str:
    try:
        sub, rg, name = (os.environ[k] for k in
                         ("ADF_SUBSCRIPTION_ID", "ADF_RESOURCE_GROUP", "ADF_FACTORY_NAME"))
    except KeyError as e:
        raise AdfError(f"{e.args[0]} is not set") from None
    return (f"{MGMT}/subscriptions/{sub}/resourceGroups/{rg}"
            f"/providers/Microsoft.DataFactory/factories/{name}")


def _token() -> str:
    if os.environ.get("ADF_ACCESS_TOKEN"):
        return os.environ["ADF_ACCESS_TOKEN"]
    try:
        out = subprocess.run(
            ["az", "account", "get-access-token", "--resource", MGMT,
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=30, check=True)
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        raise AdfError("no Azure token: set ADF_ACCESS_TOKEN or run `az login`") from e
    return out.stdout.strip()


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https host
            return json.load(resp)
    except OSError as e:
        raise AdfError(f"ADF request failed: {e}") from e


def _window(hours: float) -> dict:
    now = datetime.now(timezone.utc)
    return {"lastUpdatedAfter": (now - timedelta(hours=hours)).isoformat(),
            "lastUpdatedBefore": now.isoformat()}


def fetch_failed_runs(hours: float = 24) -> list[dict]:
    body = {**_window(hours),
            "filters": [{"operand": "Status", "operator": "Equals", "values": ["Failed"]}]}
    return _post(f"{_factory_base()}/queryPipelineRuns?api-version={API_VERSION}",
                 body).get("value", [])


def fetch_activity_runs(run_id: str, hours: float = 24 * 7) -> list[dict]:
    body = {**_window(hours)}
    return _post(f"{_factory_base()}/pipelineruns/{run_id}/queryActivityruns"
                 f"?api-version={API_VERSION}", body).get("value", [])


def _error_text(err) -> str:
    if isinstance(err, dict):
        code, msg = err.get("errorCode", ""), err.get("message", "")
        return f"{code}: {msg}".strip(": ") if (code or msg) else json.dumps(err)
    return str(err or "")


def build_log_text(run: dict, activities: list[dict]) -> str:
    """Render an ADF run and its failed activities as a plain-text log for triage."""
    lines = [
        f"ADF pipeline run: {run.get('pipelineName', '?')} (runId {run.get('runId', '?')})",
        f"Status: {run.get('status', 'Failed')}  "
        f"Start: {run.get('runStart', '?')}  End: {run.get('runEnd', '?')}",
    ]
    if run.get("message"):
        lines.append(f"ERROR pipeline: {run['message']}")
    for a in activities:
        if a.get("status") != "Failed":
            continue
        lines.append(f"ERROR activity '{a.get('activityName')}' ({a.get('activityType')}): "
                     f"{_error_text(a.get('error'))}")
        output = a.get("output")
        if output:
            lines.append(f"  output: {json.dumps(output)[:2000]}")
    return "\n".join(lines)


def _load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except (OSError, ValueError):
        return set()


def _save_seen(seen: set[str]) -> None:
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen)))
    except OSError as e:
        print(f"note: could not write {SEEN_FILE}: {e}", file=sys.stderr)


def triage_run(run: dict, activities: list[dict] | None = None):
    """Triage one ADF run; records history/DB like any other run. Returns the Triage."""
    from Triage import _append_history, run_triage

    text = build_log_text(run, activities or [])
    name = f"adf:{run.get('pipelineName', '?')}:{run.get('runId', '?')}"
    result = run_triage(name, text)
    _append_history(name, result)
    return result


def poll(hours: float = 24) -> int:
    seen = _load_seen()
    done = 0
    for run in fetch_failed_runs(hours):
        run_id = run.get("runId")
        if not run_id or run_id in seen:
            continue
        triage_run(run, fetch_activity_runs(run_id))
        seen.add(run_id)
        _save_seen(seen)
        done += 1
        print(f"triaged {run.get('pipelineName')} {run_id}")
    print(f"{done} new failed run(s) triaged")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Triage failed Azure Data Factory runs")
    ap.add_argument("--hours", type=float, default=24, help="look-back window (default 24)")
    args = ap.parse_args(argv)
    try:
        return poll(args.hours)
    except AdfError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
