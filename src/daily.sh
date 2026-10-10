#!/bin/bash
# Daily maintenance: capture today's schedule before it expires, distil finished
# archives into pairs, score the closed days, drop raw archives we no longer need.
#
# This runs on the capture host because everything it reads -- data/live, data/pairs,
# data/sched_full -- is gitignored and lives here, not in the repo. The GitHub
# workflows duplicate only the suite, which is the part that needs no archive.
#
# It does not commit or push. publish.py --commit is a deliberate step: see the
# reminder at the end.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
# snapshot_schedule.py talks to api-v3 and reads MBTA_API_KEY from here.
for f in ops/ntfy.env ops/secrets.env; do
  [ -f "$f" ] && set -a && . "$f" && set +a
done
echo "=== $(date -Iseconds) daily ==="
uv run --quiet python src/snapshot_schedule.py
uv run --quiet --with polars python src/rollup.py --compact --prune 90
# Scores any closed day not already in data/scores.jsonl, then republishes the
# window. The scoreboard is why the panel keeps its history after the prune above
# deletes the archives it was computed from.
uv run --quiet --with polars --with numpy python src/stats.py
# LAMP publishes a day or two behind, so keep asking for the recent window rather
# than only yesterday: a day this misses is a permanent hole in the heatmap.
uv run --quiet python src/fetch_history.py \
  "$(date -v-10d +%F 2>/dev/null || date -d '10 days ago' +%F)" "$(date +%F)"
# Written to data/, served by the backend's /figures -- not published. Nothing in
# the origin moves nightly, so this needs no commit and no push.
uv run --quiet --with polars python src/figures.py
echo "--- regression tests ---"
uv run --quiet --with pytest --with numpy --with polars python -m pytest tests/ -q
echo "--- static origin ---"
uv run --quiet python src/publish.py --check
if [ -n "$(git status --porcelain web data/stats.json data/scores.jsonl)" ]; then
  echo
  echo "the origin has moved since the last publish:"
  git status --short web data/stats.json data/scores.jsonl
  echo "  uv run python src/publish.py --commit    # then push; Pages deploys web/"
fi
echo "=== done ==="
