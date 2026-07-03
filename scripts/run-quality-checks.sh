#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$repo_root/.uv-cache}"

cd "$repo_root"

uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run python scripts/check_docstrings.py
git diff --check

