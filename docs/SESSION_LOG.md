# Session log: hosting Pipeline Log Triage on Vercel + Azure

Date: 2026-09-23 to 2026-09-24. No secrets are recorded here; where a value is
needed it is described by name only.

## Goal

Host the triage UI on Vercel and stop depending on anything running on the
owner's laptop, then extend the app with more features.

## Final architecture

```
browser --> Vercel (static UI in web/ + dashboard API in api/index.py)
              |-- dashboard/login/findings --> Neon Postgres (DATABASE_URL)
              \-- triage requests ----------> Azure App Service container
                                                (azure_app/main.py, runs `claude -p`
                                                 on the owner's Claude subscription)
                                                 \--> saves runs to the same Neon DB
```

| Piece | Where | Notes |
| --- | --- | --- |
| UI + dashboard API | https://pipeline-log-triage.vercel.app | Auto-deploys from `main` (Vercel project `pipeline-log-triage`) |
| Triage backend | https://pipeline-log-triage-saravanan.azurewebsites.net | Linux container, B1 plan, **West US 2** |
| Database | Neon Postgres | Created through Vercel Storage; seeded with `seed_db.py` |
| Image registry | Azure Container Registry `pipelinelogtriageacr` | Image `pipeline-log-triage-web:latest` |
| Resource group | `pipeline-log-triage-rg` | Delete it to stop all Azure charges |

## Timeline

1. **Local run attempt.** Started `local_server.py` on port 8000. Dashboard said
   "database not configured - set DATABASE_URL". Local Postgres 14 was running but
   the `postgres` password was unknown, so local seeding failed. Local server was
   stopped later at the owner's request.
2. **Vercel.** The UI is static (`vercel.json` publishes `web/`), so nothing to
   build. Pushing `main` triggered a Production deployment.
3. **Neon.** Created via Vercel Storage, `DATABASE_URL` injected. Seeded from the
   owner's terminal. The dashboard first still said "database not configured"
   because the deployment predated the env var; redeploying fixed it.
4. **Decision: no laptop dependency.** Vercel cannot use the Claude subscription,
   so triage moved to an Azure container (the repo's existing `azure_app/`).
5. **Azure setup.** Registered the `Microsoft.ContainerRegistry` and
   `Microsoft.Web` resource providers, created the registry, built the image with
   `az acr build`, and created the web app. B1 quota was 0 in East US, so the plan
   is in West US 2. The image name was briefly doubled (registry host repeated)
   and was fixed by setting `linuxFxVersion` and the registry app settings.
6. **Wiring the UI to Azure.** Added a JSON `POST /api/triage` to the Azure app
   with CORS for the Vercel origin and an access code; the UI now defaults to the
   Azure URL on `*.vercel.app`.
7. **Claude credential.** Base64-encoding `~/.claude.json` was rejected by Azure
   (about 81 KB, too large for an app setting; on macOS the login also lives in
   the Keychain). Used `claude setup-token` instead and set
   `CLAUDE_CODE_OAUTH_TOKEN` on the app. Triage then worked end to end.
8. **Findings not appearing.** Runs were not saved because the Azure image lacked
   `triage_db.py` and `DATABASE_URL`. Added `COPY triage_db.py` (and later
   `notify.py`) to the Dockerfile and set `DATABASE_URL` on the app; findings
   then appeared on the dashboard.
9. **UI customisation.** Added a persistent background colour picker to both
   pages (localStorage; text and card colours adapt automatically).
10. **Improvements implemented** (see below).
11. **Shared session key.** `TRIAGE_SECRET_KEY` in Vercel was marked Sensitive
    and could not be read back, so a new key was generated, set on Azure, and
    pasted into Vercel by the owner. Sign-in on both the dashboard and the
    triage page was confirmed working by the owner.
12. **Slack alerts.** The owner created a Slack app with an Incoming Webhook and set
    `TRIAGE_WEBHOOK_URL` on the Azure app. A test triage (`slack-alert-test`, an
    out-of-memory failure rated critical) produced an alert in the channel,
    confirmed by the owner. (A stray `invalid_payload` seen earlier came from
    testing the webhook outside the app; Slack only accepts a JSON POST.)
13. **Email alerts.** The owner set the SMTP variables on the Azure app and
    replaced the seeded placeholder `oncall_email` addresses in the `teams` table
    with real ones. A test triage (`email-alert-test`, an S3 permission failure)
    produced an email to the Platform & Infrastructure on-call address, confirmed
    by the owner.

## Improvements implemented

- **Severity, confidence, suggested fix** per failure (`Failure` model, prompt,
  DB priority mapping; the first failure is never below P1). Suggested fixes are
  labelled as AI suggestions.
- **Repeat detection** via a per-finding `signature`; dashboard shows "Nx" and a
  recurring-failures list. `triage_db.ensure_migrated` adds columns automatically.
- **Alerts** (`notify.py`): Slack/Teams webhook and optional SMTP email for
  P0/P1 or 3x-repeat findings. Best-effort.
- **CI ingest**: `Authorization: Bearer <TRIAGE_INGEST_TOKEN>` on the Azure API;
  example workflow in `docs/github-action-example.yml`.
- **Auth and rate limiting** on the Azure API: CI token, signed-in user token, or
  shared access code; 10 requests per minute per caller (in memory).
- **Input handling**: UTF-16 logs decode correctly; oversized logs keep
  error-like lines from the omitted middle.
- **Trends and cost** panel and per-finding **Markdown export**; triage page can
  download a report or print to PDF.

Design notes are in `DESIGN.md` under "Triage improvements".

## Configuration reference (names only)

Vercel: `DATABASE_URL`, `TRIAGE_SECRET_KEY`.

Azure app settings: `WEBSITES_PORT=8000`, `DOCKER_REGISTRY_SERVER_URL/USERNAME/PASSWORD`,
`CLAUDE_CODE_OAUTH_TOKEN`, `TRIAGE_ACCESS_CODE`, `TRIAGE_ALLOWED_ORIGINS`,
`TRIAGE_INGEST_TOKEN`, `DATABASE_URL`, `TRIAGE_SECRET_KEY` (must equal Vercel's).
Optional: `TRIAGE_WEBHOOK_URL`, `TRIAGE_SMTP_HOST`/`FROM`/`PORT`/`USER`/`PASSWORD`,
`TRIAGE_RATE_LIMIT`.

Redeploy Azure after code changes:

```bash
az acr build --registry pipelinelogtriageacr --image pipeline-log-triage-web:latest --file azure_app/Dockerfile .
az webapp restart -g pipeline-log-triage-rg -n pipeline-log-triage-saravanan
```

## Verified vs not verified

Verified: build gate clean (144 tests); Vercel deploys from `main`; Azure
`/api/triage` returns structured results with severity, confidence and a suggested
fix; access-code, CI-token and rate-limit paths; a triage run saved to Neon and
visible on the dashboard; sign-in and triage working on both pages and a Slack
Slack alert and an email alert delivered (all confirmed by the owner).

Not verified: the trends panel,
Markdown export and print/PDF buttons in a browser; the GitHub Action example
against a real repo; cold-start latency of the Azure container.

## Open items and cautions

- Slack and email alerts are both on. Email goes to each team's `oncall_email`
  in the `teams` table, so keep those addresses current.
- The rate limiter is per instance and resets on restart.
- The Azure B1 plan and registry are billed; the Claude OAuth token from
  `claude setup-token` lasts a year. Revoke it if the deployment is torn down or
  the token may have leaked. The token was displayed in the owner's terminal
  once; clear that scrollback.
- Check that running the personal subscription from a hosted container fits
  Anthropic's terms.
- The Azure container's `.triage_history.jsonl` is not durable across restarts;
  Postgres is the durable record.
- The first triage the owner ran before `DATABASE_URL` was set on Azure was never
  saved and cannot be recovered.
- `local_server.py` was stopped; nothing in the deployed setup depends on it.
