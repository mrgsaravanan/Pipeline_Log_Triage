# Azure deployment - Pipeline Log Triage web app

A thin FastAPI wrapper around [`../Triage.py`](../Triage.py): paste or upload
a log, get the same structured triage report back as HTML instead of stdout.
Deployed to **Azure App Service** as a custom Docker container (not Azure
Functions - see the "Why App Service, not Functions" note below).

## Known uncertainty - verify before relying on this

The Dockerfile installs the `claude` CLI via `npm install -g
@anthropic-ai/claude-code`, which is the common distribution method - but
this was **not verified against your specific local install**. The `claude`
binary on this Mac is a compiled native executable (`Mach-O 64-bit arm64`),
which suggests it may have come from a different install path than plain
npm. If `docker build` fails on the `claude --version` step, the package
name or install method needs adjusting - check
[claude.com/code](https://claude.com/code) or your own install history for
how this CLI actually got onto this machine.

## Why App Service, not Azure Functions

Functions (serverless/Consumption) instances are ephemeral - they get torn
down and recycled between invocations, with no reliable persistent
filesystem to hold an authenticated `claude` CLI session. App Service runs a
persistent container, so the credential file written at startup (see below)
survives for the container's lifetime. This is still an unusual
architecture for a hosted service - see the tradeoff discussion in the
chat history that led here.

## Credential handling - what actually happens, and why

The `claude` CLI needs its account credential file (`~/.claude.json` on your
Mac) to authenticate. That file is **never** committed to git, **never**
baked into the Docker image (image layers sit in a container registry
indefinitely, so anything COPYed in would too), and **never** typed into
this chat / handled by Claude Code directly. Instead:

1. **You** encode it yourself, locally, and copy the result straight to your
   clipboard - it never touches this terminal's output or gets read into
   any conversation:

   ```bash
   base64 -i ~/.claude.json | pbcopy
   ```

2. **You** paste that value into the Azure Portal yourself (Web App ->
   Configuration -> Application settings -> New application setting), as an
   app setting named `CLAUDE_CREDENTIALS_B64`. Or via the CLI, still without
   it ever landing in a file or a chat message:

   ```bash
   az webapp config appsettings set \
     --resource-group <your-resource-group> \
     --name <your-app-name> \
     --settings CLAUDE_CREDENTIALS_B64="$(base64 -i ~/.claude.json)"
   ```

3. At container **startup** (not build time), `docker-entrypoint.sh` reads
   that app setting, base64-decodes it, and writes `/root/.claude.json`
   inside the running container - then launches the app. Restarting or
   redeploying the container re-runs this step; the raw file only ever
   exists in the running container's ephemeral filesystem, never in the
   image itself.

This is still a real tradeoff: a personal Claude subscription credential now
lives in an Azure App Setting (visible to anyone with Contributor+ access to
this Web App in the Portal/CLI/ARM export). That's a deliberate choice made
after discussing the alternative (a billed `ANTHROPIC_API_KEY`, which has
the same "lives in Azure config" exposure but is a scoped, revocable API
key rather than a personal account session).

## Deploy steps

Prerequisites: an Azure subscription on `mrgsaravanan@gmail.com`, and the
Azure CLI. If `az` isn't installed and Homebrew isn't available on this
machine, install it via pip instead:

```bash
pip install azure-cli
az login   # opens a browser - sign in as mrgsaravanan@gmail.com
```

Then, from the **repo root** (not this folder - the Dockerfile needs the
whole repo as its build context):

```bash
# One-time resource setup - adjust names/region as you like.
az group create --name pipeline-log-triage-rg --location eastus
az acr create --resource-group pipeline-log-triage-rg \
  --name pipelinelogtriageacr --sku Basic
az acr login --name pipelinelogtriageacr

# Build and push the image (ACR Tasks builds in the cloud - no local Docker
# daemon required; use `docker build`/`docker push` instead if you'd rather
# build locally).
az acr build --registry pipelinelogtriageacr \
  --image pipeline-log-triage-web:latest \
  --file azure_app/Dockerfile .

# App Service plan + Linux Web App running that image.
az appservice plan create --resource-group pipeline-log-triage-rg \
  --name pipeline-log-triage-plan --is-linux --sku B1
az webapp create --resource-group pipeline-log-triage-rg \
  --plan pipeline-log-triage-plan --name <pick-a-unique-app-name> \
  --deployment-container-image-name pipelinelogtriageacr.azurecr.io/pipeline-log-triage-web:latest

# Point the app at port 8000 (matches the Dockerfile's EXPOSE/CMD).
az webapp config appsettings set --resource-group pipeline-log-triage-rg \
  --name <your-app-name> --settings WEBSITES_PORT=8000

# The credential setting from step 2 above:
az webapp config appsettings set --resource-group pipeline-log-triage-rg \
  --name <your-app-name> --settings CLAUDE_CREDENTIALS_B64="$(base64 -i ~/.claude.json)"
```

Then visit `https://<your-app-name>.azurewebsites.net`.

## Known limitations (carried over / new)

- Everything in [`../DESIGN.md`](../DESIGN.md)'s log-varieties and
  persistence sections still applies (UTF-16 gap, unbounded history file) -
  `.triage_history.jsonl` here lives inside the container's filesystem,
  which is not guaranteed to survive a redeploy or a plan change on Azure
  App Service Linux (some tiers persist `/home`, some don't - verify for
  your plan rather than relying on it for anything important).
- No auth, no rate limiting on this page. Before making the URL genuinely
  public, add both - an unauthenticated page that triggers a `claude` CLI
  call per request has no natural cost/abuse ceiling.
- The `claude` CLI install step in the Dockerfile is unverified (see above)
  - budget time for fixing the install command on first deploy.

## Azure Data Factory endpoints

The container also exposes the ADF integration from [`../adf.py`](../adf.py)
(see DESIGN.md's "Azure Data Factory integration"). Both need `ADF_WEBHOOK_SECRET`
set as an App Service application setting and sent in the `X-ADF-Secret` header.

- `POST /api/adf/webhook` - point an ADF Web activity on the pipeline's Failure path
  at `https://<app>.azurewebsites.net/api/adf/webhook`.
- `POST /api/adf/poll?hours=24` - triages failed runs not seen before; call it on a
  schedule (Logic App recurrence, cron). Only `hours` up to 168 is honored.

To let the container read the factory, set `ADF_SUBSCRIPTION_ID`, `ADF_RESOURCE_GROUP`,
`ADF_FACTORY_NAME`, enable the App Service **system-assigned managed identity**, and grant
it the *Data Factory Contributor* role (or a custom role with
`Microsoft.DataFactory/factories/querypipelineruns/action` and
`.../pipelineruns/queryactivityruns/action`) on the factory. No secret is stored.

Limitations: the "already triaged" list (`.adf_seen.json`) lives on the container's
ephemeral disk, so a restart can re-triage recent runs; the webhook and poll calls run
synchronously and can exceed ADF's Web activity timeout if Claude is slow.

## MCP server endpoint

The container also mounts [`../mcp_server.py`](../mcp_server.py) at `/mcp/` (trailing
slash required - it's a sub-app mount, so `/mcp` alone 307-redirects there) when
`MCP_SERVER_TOKEN` is set as an App Service application setting:

```bash
az webapp config appsettings set --resource-group pipeline-log-triage-rg \
  --name <your-app-name> --settings MCP_SERVER_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

then rebuild and redeploy (the `az acr build` step above) so the new image installs
`mcp` (in `azure_app/requirements.txt`) and picks up the setting on restart. A remote
MCP client connects to `https://<your-app-name>.azurewebsites.net/mcp/` with header
`Authorization: Bearer <the MCP_SERVER_TOKEN value>`.

This makes `triage_log` (which spends this app's Claude subscription per call) and the
findings-dashboard tools reachable from the public internet for as long as the app is
up, gated only by that one bearer token - there is no per-user login the way the
dashboard has. Leave `MCP_SERVER_TOKEN` unset to leave `/mcp` unmounted (a plain 404)
if you don't need this. See `../mcp_server.py`'s module docstring and
`../CLAUDE.md`'s "MCP server" section for the same tradeoff as running it locally in
`--transport http` mode, just permanent instead of ad hoc.
