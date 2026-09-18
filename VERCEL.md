# Hosting on Vercel

The hosted version is the same FastAPI app as `azure_app/main.py`, served by
Vercel via `[tool.vercel] entrypoint` in `pyproject.toml`, with a 60s function
limit set in `vercel.json`. On Vercel it automatically uses the **Anthropic
API** backend (Vercel sets `VERCEL=1`); local runs stay on the `claude` CLI.

The code side is done. These steps are **yours** - they involve your accounts
and secrets, which Claude Code doesn't handle:

1. **API key + spend cap.** At [console.anthropic.com](https://console.anthropic.com)
   → API Keys, create a key, and set a monthly **spend limit** first (Settings →
   Limits). This cap is your real protection against runaway cost.
2. **Create the Vercel project.** vercel.com → Add New → Project → import
   `mrgsaravanan/Pipeline_Log_Triage` (this links GitHub to Vercel; framework
   should auto-detect as FastAPI/Python). Don't deploy yet if it prompts for
   env vars first - set them in step 3.
3. **Environment variables** (Project → Settings → Environment Variables,
   Production + Preview):
   - `ANTHROPIC_API_KEY` = your key (never commit it, never paste it in chat)
   - `TRIAGE_ACCESS_CODE` = a passphrase of your choice. **Strongly
     recommended**: without it anyone with the URL can spend your API budget.
4. **Deploy** (or Redeploy after adding env vars - they apply to new builds only).
5. **Test the live URL** with a sample log (e.g. paste the contents of
   `sample_multi_failure.log`) before sharing it.

## Cost, roughly

Model is `claude-haiku-4-5` ($1 / $5 per million input / output tokens): a
typical triage is well under a cent; the worst case (a log at the 200k-char
truncation cap) is about $0.05. The Vercel Hobby tier itself is free.

## Known limits

- No real rate limiting (see DESIGN.md) - rely on the access code + spend cap.
- Vercel request bodies are capped at ~4.5 MB; larger uploads are rejected
  by the platform before reaching the app.
- History (`.triage_history.jsonl`) is not persisted on Vercel.
- Screenshots (PNG/JPEG/GIF/WebP, up to 4 MB) are supported on Vercel via the
  API backend. Each image costs a little more than text (roughly a cent or
  less on Haiku). Accuracy depends on how legible the screenshot is.
- Not yet verified on a real Vercel deployment - the first deploy may need
  tweaks (see the checklist above and report any build error).
