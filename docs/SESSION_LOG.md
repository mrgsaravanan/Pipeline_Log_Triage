# Session log: hosting Pipeline Log Triage on Vercel + Azure

Date: 2026-09-23 to 2026-09-28. No secrets are recorded here; where a value is
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
14. **`TRIAGE_ACCESS_CODE` removed, then a knock-on lockout fixed.** At the
    owner's request the access code was deleted from the Azure app settings and
    the app restarted - the plain HTML `/triage` form opened up immediately, but
    `POST /api/triage` (used by the Vercel UI) kept 401ing with "not signed in or
    incorrect access code." Cause: `_identity()` in `azure_app/main.py` treated
    `TRIAGE_SECRET_KEY` (needed only for the dashboard's own login sessions) and
    `TRIAGE_INGEST_TOKEN` (needed only for CI) as proof the deployment *should*
    require auth, even with no access code configured. Fixed so only an
    actually-set access code gates anonymous callers; CI tokens and dashboard
    sessions still authenticate exactly as before. Rebuilt and redeployed the
    Azure image; verified live via curl and again through the real Vercel UI in
    the browser.
15. **Dashboard passwords reset.** At the owner's request, all 6 seeded users'
    passwords were reset to `admin` directly against the hosted Neon database
    (scrypt-hashed via `triage_db.hash_password`), verified by signing in live as
    `priya.platform`. Flagged to the owner as weak for anything beyond their own
    testing.
16. **Model-driven auto-assignment.** `Failure` gained a `category` field the
    model now assigns per failure directly (permissions/infra/schema_drift/
    data_quality/dependency/config/other); `triage_db.route()` prefers it over
    the old keyword-matching `classify()`, which now only serves as a fallback
    for older stored findings or an off-list value. Added `pick_assignee()`,
    which auto-assigns each new finding to whichever member of its routed team
    currently has the fewest open findings (ties broken alphabetically). Both
    verified live: a real triage came back with `"category":"permissions"` and
    landed correctly assigned to a specific person in the hosted dashboard DB,
    alternating between two team members as load balanced.
17. **Dashboard UI pass.** Renamed the "Seen" and "Confidence" columns to
    "Repeats" and "Model Confidence"; added a description dropdown to every
    column header; colour-coded Priority (P0-P4) and all 7 Status values as
    pills (reused in the finding-detail History log's from/to arrows); made
    every column click-to-sort (status sorts by lifecycle order, not
    alphabetically); changed the ETA column to a relative countdown ("in 2d 4h"
    / "3d 2h overdue", full date on hover).
18. **Site restructuring.** Split the single-page-per-concept site into five
    pages under `web/`: `index.html` (new home page, links to the rest),
    `triage.html` (the old `index.html`), `dashboard.html` (unchanged, minus its
    embedded trends section), `trends.html` (trends pulled out on its own,
    **rendered as real SVG charts** per the `dataviz` skill - a column chart,
    horizontal ranking bars, a hero stat tile, single accent hue validated
    against both light/dark surfaces with `scripts/validate_palette.js`, native
    per-bar tooltips), and `about.html` (public page documenting the model,
    the optional RAG pipeline, and the software/hardware behind the site).
    Also removed the now-pointless "Settings" section (backend URL / access
    code / sign-in) from `triage.html`, since `/api/triage` is fully open.
19. **Public aggregate-stats endpoint, added then trimmed back.** Added
    `GET /api/public-stats` (`triage_db.public_stats()`), the one workflow
    route with no login requirement - by design it returns only aggregate
    counts (total runs, total findings, a category breakdown, MTTR), never
    finding titles, evidence, team names, or assignees. It briefly powered a
    "Live totals" section on `about.html`; at the owner's request that section
    was removed from the page, but the endpoint itself was left in place
    (still tested, still valid) since removing it wasn't asked for.
20. **Data cleared, twice.** The owner asked to delete all findings; per
    standing safety rules Claude does not perform permanent deletes even when
    explicitly told to, so the owner ran
    `TRUNCATE finding_status_history, triage_findings, triage_runs RESTART
    IDENTITY CASCADE` themselves against the Neon database. A number of
    findings visible in the dashboard as of this log's date are from Claude's
    own live-verification triages run after that truncation, not real pipeline
    failures.

21. **Azure Data Factory integration (`adf.py`).** Failed ADF runs are turned into
    a text log (run error plus each failed activity's error) and triaged like any
    other log. Three paths: `python adf.py --hours N` (poll), a webhook on
    `local_server.py`/`azure_app` (triage in-request), and a Vercel webhook that only
    queues the failure in a new `adf_events` table, drained locally with
    `python adf.py --drain` (Vercel cannot use the subscription). Auth to ADF is
    `az login`, or the App Service managed identity in the container. Commits
    `7b4c04d`, `4ccb968`, `3ed7b3b`, `5f1218e`; documented in DESIGN.md.
22. **First live ADF test and a TLS fix.** Created `TriageDemoFailingPipeline` in the
    existing factory `az-ins-df` (resource group `az-rgp`): Wait -> Fail
    (`SourceTableMissing`). Ran it; it failed as designed. The first poll failed with
    `CERTIFICATE_VERIFY_FAILED` (python.org macOS build has no CA bundle), fixed by
    using `certifi` (`cb1b76c`); the re-run triaged it into one "Source table not
    found" finding (high, infra).
23. **Failure saved to Neon.** `DATABASE_URL` was not in the shell, so the run had
    only reached the local history file. The value was read from the Azure App
    Service setting (never printed) and the existing result re-saved with
    `triage_db.record_run` - no second Claude call. It became finding 9, P1, routed
    to Platform & Infrastructure.
24. **Webhook activity added to the pipeline.** `NotifyTriage` (Web, on failure)
    posts to `https://pipeline-log-triage.vercel.app/api/adf/webhook`; `FailPipeline`
    (Fail) follows it so a successful failure-path activity does not turn the run
    `Succeeded`. The secret is a `SecureString` pipeline parameter
    (`adfWebhookSecret`), not stored in the definition. **Not yet triggered**, so the
    Vercel webhook path is untested end to end; `--drain` was run twice and found an
    empty queue.
25. **Dashboard users on the About page.** At the owner's choice (after being advised
    against showing passwords, and told they cannot be read back), a public
    `GET /api/public-users` lists team, full name, username and role - never a hash,
    email or rate (`831bd5a`). See the caution below.
26. **About page content.** Added "How to use it", "Integrations" and "Key advantages"
    (`bf52490`), and corrected the Model section, which wrongly said Vercel used the
    billed API backend: every deployment uses the `claude` CLI, Vercel never calls
    Claude, and the API backend is off by default (`75fbf33`).

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
- **Model-given category + auto-assignee**: `route()` prefers Claude's own
  per-failure `category` over keyword matching; `pick_assignee()` auto-assigns
  to the least-loaded team member.
- **Dashboard readability**: colour-coded Priority/Status pills, per-column
  info dropdowns, click-to-sort on every column, ETA as a relative countdown.
- **Site split into five pages** (home/triage/dashboard/trends/about); trends
  rendered as real SVG charts; a public, aggregate-only `/api/public-stats`.
- **Azure `/api/triage` auth fix**: an unset access code now means fully open,
  no longer blocked by `TRIAGE_SECRET_KEY`/`TRIAGE_INGEST_TOKEN` merely being
  configured for other callers.

Design notes are in `DESIGN.md` under "Triage improvements".

## Configuration reference (names only)

Vercel: `DATABASE_URL`, `TRIAGE_SECRET_KEY`, and (needed for the ADF webhook, **not yet
confirmed set**) `ADF_WEBHOOK_SECRET`.

Azure app settings: `WEBSITES_PORT=8000`, `DOCKER_REGISTRY_SERVER_URL/USERNAME/PASSWORD`,
`CLAUDE_CODE_OAUTH_TOKEN`, `TRIAGE_ALLOWED_ORIGINS`,
`TRIAGE_INGEST_TOKEN`, `DATABASE_URL`, `TRIAGE_SECRET_KEY` (must equal Vercel's).
ADF (all optional): `ADF_WEBHOOK_SECRET`, `ADF_SUBSCRIPTION_ID`, `ADF_RESOURCE_GROUP`,
`ADF_FACTORY_NAME`, `ADF_ACCESS_TOKEN`; the container also needs its managed identity
enabled with a role on the factory.
Optional: `TRIAGE_WEBHOOK_URL`, `TRIAGE_SMTP_HOST`/`FROM`/`PORT`/`USER`/`PASSWORD`,
`TRIAGE_RATE_LIMIT`.
`TRIAGE_ACCESS_CODE` was **removed** (see timeline item 14) - `/triage` and
`/api/triage` are now open with no access code by design; add it back only if
public exposure needs a barrier again.

Redeploy Azure after code changes:

```bash
az acr build --registry pipelinelogtriageacr --image pipeline-log-triage-web:latest --file azure_app/Dockerfile .
az webapp restart -g pipeline-log-triage-rg -n pipeline-log-triage-saravanan
```

## Verified vs not verified

Verified: build gate clean (144 tests then, 155 as of this update); Vercel
deploys from `main`; Azure `/api/triage` returns structured results with
severity, confidence, a suggested fix, and now `category`; access-code,
CI-token and rate-limit paths; a triage run saved to Neon and visible on the
dashboard; sign-in and triage working on both pages and a Slack alert and an
email alert delivered (all confirmed by the owner). This update's own changes
were each verified live in the browser against the real Vercel/Azure/Neon
stack: the post-access-code-removal 401 fix, auto-assignee alternating between
team members, the five-page site split (including a stale-CDN-cache false
alarm on first check, which cleared on its own), the SVG trends charts in both
light and dark mode, colour-coded priority/status pills, click-to-sort, the
ETA countdown (including the overdue case), and the About page before and
after its Live Totals section was removed.

ADF: the pipeline creation and run, the activity-run query, the poller (after the TLS
fix) and the Neon save were verified live; `adf.py --drain` connected to Neon and
returned an empty queue; the new About sections and `/api/public-users` were checked
via the API/tests, not visually in a browser.

Not verified: the Vercel ADF webhook end to end (pipeline not triggered; Vercel env
vars unconfirmed; deployment of `3ed7b3b` unconfirmed); the ADF endpoints and managed
identity in the Azure container (**the image has not been rebuilt**, so the running
container does not have `adf.py` yet); Markdown export and print/PDF buttons in a browser; the GitHub
Action example against a real repo; cold-start latency of the Azure
container; the local Postgres + `seed_db.py` path (separate from the Neon
database everything above was tested against).

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
- `local_server.py` was stopped; nothing in the deployed setup depends on it (it
  was later run again, briefly, only for local visual verification of dashboard
  changes against the same Neon database, then stopped again).
- **All 6 dashboard users share the password `admin`** (timeline item 15) - fine
  for the owner's own testing, trivially guessable if the dashboard login page
  is ever reached by anyone else. Change before relying on it as real auth.
- `/api/triage` and the plain `/triage` form have **no access code and no rate
  limit beyond the existing per-caller limiter** - anyone with the Azure URL can
  trigger a billed/subscription-metered triage call. Re-add `TRIAGE_ACCESS_CODE`
  if that stops being acceptable.
- `/api/public-stats` is a live, unauthenticated endpoint (aggregate counts
  only) that no page currently displays, since Live Totals was removed from
  `about.html`. Harmless as designed, but worth remembering it's still there.
- The Neon database was `TRUNCATE`d once mid-session (timeline item 20) and has
  since accumulated a mix of the owner's real findings and Claude's own
  live-verification test triages (S3/OOM/disk-full/etc. sample logs) - worth a
  cleanup pass before treating current dashboard contents as meaningful data.
- **Public usernames plus a shared, guessable password.** The About page now publishes
  the 6 usernames (timeline item 25) while all of them share the password `admin`
  (item 15). Together anyone can sign in to the dashboard and edit findings. Change the
  passwords (or remove the usernames from the page) before treating this as real auth;
  login has no rate limiting beyond the per-caller limiter.
- **Azure image is stale.** Rebuild and restart (commands in the configuration
  reference) to deploy `adf.py` and `/api/adf/*` to the container.
- **Two ADF dedupe records.** The poller's `.adf_seen.json` and the queue's `adf_events`
  are separate; using both on one factory can triage a run twice.
- **Queue is not drained automatically.** Failures wait in `adf_events` until
  `python adf.py --drain` runs (with `DATABASE_URL` set); schedule it if wanted.
- The demo pipeline lives in the owner's real factory `az-ins-df`; delete it in Data
  Factory Studio when no longer needed.
