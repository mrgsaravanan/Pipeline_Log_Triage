"""MCP server exposing Pipeline Log Triage to an MCP client (e.g. Claude Desktop
or Claude Code's MCP config), so a log can be triaged - and the workflow
dashboard's findings browsed - without opening a terminal or the web UI.

Runs entirely on your own machine, same trust model as local_server.py and
adf.py: triage goes through the `claude` CLI against your own subscription
(never a billed API key - TRIAGE_BACKEND is forced to "cli" below), and the
findings-workflow tools talk to Postgres directly, with no login of their own,
because an MCP client only reaches this process if it already runs on this
machine with this environment - the same level of trust `python adf.py --drain`
or `psql "$DATABASE_URL"` already assume.

Install (separate from requirements.txt/pyproject.toml - Vercel/Azure never
need this):

    pip install -r requirements-mcp.txt

Then add it to your MCP client's config, e.g. for Claude Code:

    claude mcp add pipeline-log-triage -- \\
        /path/to/.venv/bin/python /path/to/mcp_server.py

The findings/trends tools are a no-op-with-a-clear-error when DATABASE_URL
isn't set (see DESIGN.md's "Postgres workflow" section) - triage_log itself
never needs it.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# This server exists to use the subscription; never fall through to the API.
os.environ["TRIAGE_BACKEND"] = "cli"

# mcp>=2.0 renamed FastMCP to MCPServer (same decorator-based tool API) - see
# https://py.sdk.modelcontextprotocol.io/v2/migration/#fastmcp-renamed-to-mcpserver
from mcp.server.mcpserver import MCPServer  # noqa: E402

from Triage import (  # noqa: E402
    ClaudeCliError,
    _append_history,
    _truncate_log_text,
    run_triage,
)

mcp = MCPServer("pipeline-log-triage")


def _db_unavailable() -> dict | None:
    """None if Postgres is usable; otherwise the error dict a tool should return."""
    import triage_db

    if not triage_db.db_configured():
        return {"error": f"{triage_db.DB_ENV} is not set - the findings dashboard "
                          "is optional (see DESIGN.md's Postgres workflow section)"}
    try:
        with triage_db.connect():
            pass
    except Exception as e:  # noqa: BLE001 - surfaced to the MCP client, not raised
        return {"error": f"could not connect to Postgres: {e}"}
    return None


@mcp.tool()
def triage_log(log_text: str, name: str = "(mcp input)") -> dict:
    """Triage a pipeline log into distinct root-cause failures using Claude.

    Runs through the local `claude` CLI (your Claude subscription, never a
    billed API key) - identical to `python Triage.py <file>`. Also appended to
    the local history file and, when DATABASE_URL is set, saved to the
    workflow dashboard's Postgres database with urgent findings alerted on,
    exactly as the CLI itself does.

    Returns a dict with `failures` (each: failure_type, what_broke, evidence,
    next_step, severity, confidence, suggested_fix, category) and `notes`, or
    `{"error": ...}` if triage could not run at all.
    """
    if not log_text.strip():
        return {"error": "log_text is empty"}
    text, truncated = _truncate_log_text(log_text)
    try:
        result = run_triage(name, text)
    except FileNotFoundError:
        return {"error": "claude CLI not found - install it and run `claude login`"}
    except subprocess.TimeoutExpired:
        return {"error": "claude CLI timed out"}
    except ClaudeCliError as e:
        return {"error": f"triage failed: {e}"}
    _append_history(name, result)
    report = result.model_dump()
    if truncated:
        report["_note"] = ("the log was truncated before triage (head+tail excerpt) - "
                            "see DESIGN.md's log-varieties section")
    return report


@mcp.tool()
def list_findings(status: str = "", priority: str = "", team_id: int | None = None) -> dict:
    """List findings from the workflow dashboard, optionally filtered.

    `status`: one of new/triaged/assigned/in_progress/blocked/resolved/wont_fix,
    or "" for all. `priority`: P0-P4, or "" for all. `team_id`: 1 (Platform &
    Infrastructure), 2 (Data Engineering) or 3 (Application Engineering), or
    None for all teams. Needs DATABASE_URL - see get_trends for the same
    requirement.
    """
    err = _db_unavailable()
    if err:
        return err
    import triage_db

    with triage_db.connect() as conn:
        triage_db.ensure_migrated(conn)
        findings = triage_db.list_findings(conn, status=status, priority=priority,
                                           team_id=team_id)
    return {"findings": findings}


@mcp.tool()
def get_finding(finding_id: int) -> dict:
    """Full detail for one finding by id, including its assigned team and cost."""
    err = _db_unavailable()
    if err:
        return err
    import triage_db

    with triage_db.connect() as conn:
        triage_db.ensure_migrated(conn)
        finding = triage_db.get_finding(conn, finding_id)
    return finding or {"error": f"no finding with id {finding_id}"}


@mcp.tool()
def get_trends() -> dict:
    """Weekly finding volume, cost by team, category breakdown, mean time to
    resolve, and recurring failures - the same data as web/trends.html."""
    err = _db_unavailable()
    if err:
        return err
    import triage_db

    with triage_db.connect() as conn:
        triage_db.ensure_migrated(conn)
        return triage_db.trends(conn)


if __name__ == "__main__":
    mcp.run()
