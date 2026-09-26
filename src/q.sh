#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec uv run --quiet --with duckdb python src/q.py "$@"
