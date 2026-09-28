# Web UI: static page on Vercel + local backend on your Claude subscription

Vercel cannot use a Claude Pro/Max subscription (Anthropic does not allow
routing subscription credentials through a hosted service, and a serverless
function has no `claude login` session). So the architecture is split:

```
browser --loads--> Vercel (static web/*.html - home, triage.html, dashboard.html,
                     trends.html - no secrets, no server code)
browser --fetch--> http://localhost:8000/api/triage  (local_server.py on your Mac)
                        \-> `claude -p`  -> your Claude subscription
```

Nothing sensitive lives on Vercel; the log goes from your browser straight to
your own machine.

## Run it locally

```bash
source .venv/bin/activate
python local_server.py          # UI + API at http://localhost:8000
```

Needs the `claude` CLI installed and logged in (`claude login`).

## Deploy the UI to Vercel

1. vercel.com -> Add New -> Project -> import `mrgsaravanan/Pipeline_Log_Triage`.
2. `vercel.json` already sets framework none and output directory `web`. No env vars.
3. Deploy. Every push to `main` redeploys.
4. Start the local server allowing your Vercel URL:

```bash
TRIAGE_ALLOWED_ORIGINS="https://your-app.vercel.app" python local_server.py
```

5. Open the Vercel URL; the UI calls `http://localhost:8000`. Chrome may ask to
   allow access to local network devices - allow it.

## Notes and limits

- Works only on the machine running `local_server.py`, while it runs. It is a
  personal tool, not a shared service.
- Use Chrome, Edge or Firefox. Safari blocks HTTPS pages from calling localhost.
- The server binds to 127.0.0.1, accepts only JSON, and rejects any browser
  Origin not in `TRIAGE_ALLOWED_ORIGINS` (plus localhost), so other websites
  cannot spend your subscription.
- Screenshots go through `claude -p` with only the Read tool (see DESIGN.md).
- The old API-key Vercel deployment (`azure_app/main.py` + `anthropic`) is no
  longer wired to Vercel; that code remains for the Azure container and the
  optional `TRIAGE_BACKEND=api` mode.

## Azure Data Factory webhook on Vercel

`POST /api/adf/webhook` (in `api/index.py`) lets an ADF Web activity on a pipeline's
Failure path report a failed run to the hosted app. Vercel never calls Claude, so the route
only **queues** the failure in the `adf_events` table (Neon); the dashboard shows nothing
until it is triaged. On your machine (with `DATABASE_URL` set to the same Neon database):

    python adf.py --drain      # triages queued failures via the claude CLI, saves findings

Run it by hand or from cron/launchd. Set `ADF_WEBHOOK_SECRET` in the Vercel project; ADF
sends it as the `X-ADF-Secret` header, with body
`{"pipelineName":"@{pipeline().Pipeline}","runId":"@{pipeline().RunId}",
"message":"@{activity('<name>').error.message}","errorCode":"@{activity('<name>').error.errorCode}"}`.
Re-delivery of the same runId is ignored. `--drain` also reads the run's activity errors
from ADF when `ADF_SUBSCRIPTION_ID`/`ADF_RESOURCE_GROUP`/`ADF_FACTORY_NAME` and `az login`
are available, otherwise it triages the webhook's own error text.
