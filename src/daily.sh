#!/bin/bash
# Daily maintenance: capture today's schedule before it expires, distil finished
# archives into pairs, drop raw archives we no longer need.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
echo "=== $(date -Iseconds) daily ==="
uv run --quiet python src/snapshot_schedule.py
uv run --quiet --with polars python src/rollup.py --prune 14
echo "--- regression tests ---"
uv run --quiet --with pytest --with numpy --with polars python -m pytest tests/ -q
echo "=== done ==="
