#!/bin/sh
# Reconstructs the claude CLI's credential file from an Azure App Setting
# (env var) at container startup - never baked into the image itself, so it
# doesn't sit in a registry layer forever. See README.md for how to set
# CLAUDE_CREDENTIALS_B64 without ever pasting the secret to anyone but
# yourself, in the Azure Portal directly.
set -e

if [ -n "$CLAUDE_CREDENTIALS_B64" ]; then
    echo "$CLAUDE_CREDENTIALS_B64" | base64 -d > /root/.claude.json
    chmod 600 /root/.claude.json
else
    echo "WARNING: CLAUDE_CREDENTIALS_B64 is not set - claude CLI calls will fail auth." >&2
fi

exec "$@"
