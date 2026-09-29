#!/bin/sh
# Gives the claude CLI a credential at container startup, from an Azure App
# Setting (env var) - never baked into the image itself, so it doesn't sit in
# a registry layer forever. See README.md for how to set either of these
# without ever pasting the secret to anyone but yourself, in the Azure Portal
# directly.
#
# Preferred: CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`, run locally -
# a long-lived token the CLI reads directly as this one env var, already
# inherited by the exec'd process below, no file needed). Works regardless of
# how the local `claude` that generated it was installed (npm package, or a
# native binary that stores its own regular login in the OS keychain, which a
# plain container can never read anyway).
#
# Fallback: CLAUDE_CREDENTIALS_B64, a base64 copy of a *file-based*
# ~/.claude.json login (npm-installed claude CLIs only - a native-binary
# install's actual credential typically lives in the OS keychain instead, not
# in that file, so this only works if your local claude is npm-installed).
# Also subject to Azure App Service's app-setting size limit, which a real
# ~/.claude.json (it accumulates a lot of local cache alongside the login)
# can exceed - CLAUDE_CODE_OAUTH_TOKEN avoids that entirely.
set -e

if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
    : # already in the environment; the claude CLI reads it directly.
elif [ -n "$CLAUDE_CREDENTIALS_B64" ]; then
    echo "$CLAUDE_CREDENTIALS_B64" | base64 -d > /root/.claude.json
    chmod 600 /root/.claude.json
else
    echo "WARNING: neither CLAUDE_CODE_OAUTH_TOKEN nor CLAUDE_CREDENTIALS_B64 is set - " \
         "claude CLI calls will fail auth." >&2
fi

exec "$@"
