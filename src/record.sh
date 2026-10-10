#!/bin/bash
# Run the archiver in the agents' own environment (ops/venv.sh says why it is not
# `uv run`). Kept to one line of work on purpose: this is the process that must
# not lose data, and its launcher should have nothing in it that can go wrong.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
. ops/venv.sh
[ -x "$PY" ] || { echo "no $PY -- run ./ops/install.sh" >&2; exit 1; }
exec "$PY" src/record_rt.py
