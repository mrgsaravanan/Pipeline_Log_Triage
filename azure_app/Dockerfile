# Built from the REPO ROOT as build context (not this folder), so it can
# COPY the whole project and preserve the relative layout that
# azure_app/requirements.txt's "-r ../requirements.txt" and
# azure_app/main.py's "import Triage" both depend on:
#
#   docker build -f azure_app/Dockerfile -t pipeline-log-triage-web .
#
# NOTE ON THE `claude` CLI INSTALL BELOW: this assumes it is distributed as
# an npm package (`npm install -g @anthropic-ai/claude-code`), which is the
# common case, but was not verified against this exact local install (the
# local `claude` binary here is a compiled native executable, likely from a
# different install method - see azure_app/README.md's "Known uncertainty"
# section). Verify `claude --version` succeeds during `docker build`; if the
# package name or install method differs, this line needs adjusting.

FROM python:3.12-slim

# Node.js, needed to install/run the claude CLI (npm package).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code \
    && claude --version

WORKDIR /app

# Runtime deps (repo root's requirements.txt + this app's own).
COPY requirements.txt ./requirements.txt
COPY azure_app/requirements.txt ./azure_app/requirements.txt
RUN pip install --no-cache-dir -r azure_app/requirements.txt

# App code: the CLI script this app wraps, plus the web app itself.
COPY Triage.py ./Triage.py
COPY triage_db.py ./triage_db.py
COPY notify.py ./notify.py
COPY azure_app/main.py ./azure_app/main.py
COPY azure_app/docker-entrypoint.sh ./azure_app/docker-entrypoint.sh
RUN chmod +x ./azure_app/docker-entrypoint.sh

# Azure App Service (Linux, custom container) sends traffic to port 8000 by
# default unless WEBSITES_PORT overrides it - keep these in sync if changed.
EXPOSE 8000

ENTRYPOINT ["./azure_app/docker-entrypoint.sh"]
CMD ["uvicorn", "azure_app.main:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "8000"]
