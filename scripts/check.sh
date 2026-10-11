#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --locked ruff check
uv run --locked ruff format --check
uv run --locked mypy
uv run --locked pytest
if [[ -d agent ]]; then
    (cd agent && go vet ./... && go test ./...)
fi
