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

Remote / network mode
----------------------
By default this runs over stdio: the trust boundary is "whatever started this
process already runs on this machine." `--transport http` instead serves it
over the network for a remote MCP client (e.g. a teammate's machine, or a
different host) to connect to - but every call to triage_log spends *your*
Claude subscription, and the findings tools return real triage data with no
login of their own, so this mode is gated behind a shared bearer token that
MUST be set first:

    export MCP_SERVER_TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))")
    python mcp_server.py --transport http --host 0.0.0.0 --port 8765

A remote client then connects to http://<this-machine>:8765/mcp with header
`Authorization: Bearer <the same MCP_SERVER_TOKEN value>`. The server refuses
to start in http mode without MCP_SERVER_TOKEN set. Keep --host at its default
(127.0.0.1) unless you specifically need another machine to reach it; binding
0.0.0.0 (or a LAN/public IP) exposes it to anyone who can reach that address
and knows or guesses the token, so use a long random token, not a short or
guessable one, and treat it like any other credential (never commit it).
"""

import hmac
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


def _bearer_token_guard(app, token: str):
    """Wrap an ASGI app so every request needs `Authorization: Bearer <token>`.

    A plain shared-secret check - the same pattern as adf.secret_ok's
    X-ADF-Secret, not the mcp SDK's full OAuth support (issuer/JWKS/client
    registration), which is far more machinery than "don't let strangers on
    the network use this" needs. Constant-time comparison so response timing
    can't be used to guess the token one byte at a time.
    """
    from starlette.responses import JSONResponse

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        provided = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(provided, token):
            response = JSONResponse({"error": "bad or missing bearer token"}, status_code=401)
            return await response(scope, receive, send)
        return await app(scope, receive, send)

    return guarded


def _run_http(host: str, port: int) -> None:
    """Serve over HTTP for a remote MCP client - see this module's docstring
    for the security tradeoff. Refuses to start without MCP_SERVER_TOKEN."""
    token = os.environ.get("MCP_SERVER_TOKEN", "")
    if not token:
        print(
            "error: MCP_SERVER_TOKEN must be set to serve over --transport http "
            '(e.g. export MCP_SERVER_TOKEN=$(python -c "import secrets; '
            'print(secrets.token_hex(32))")) - see mcp_server.py\'s module docstring',
            file=sys.stderr,
        )
        raise SystemExit(1)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: binding to {host} exposes this server to more than just this "
              "machine - make sure that is intended and MCP_SERVER_TOKEN is a long, "
              "random value.", file=sys.stderr)

    import uvicorn

    app = _bearer_token_guard(mcp.streamable_http_app(), token)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="stdio (default): a local MCP client starts this process "
                             "itself. http: serve over the network - see module docstring.")
    parser.add_argument("--host", default="127.0.0.1", help="--transport http only")
    parser.add_argument("--port", type=int, default=8765, help="--transport http only")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()
    else:
        _run_http(args.host, args.port)
