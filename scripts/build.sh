#!/usr/bin/env bash
# Build gate for this project: lint + unit tests, no live API calls.
# Exit code 0 means "clean" - safe to commit/push. Non-zero means don't.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "== ruff lint =="
ruff check .

echo "== pytest =="
pytest -q

echo "== build clean =="
