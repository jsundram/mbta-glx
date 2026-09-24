#!/bin/bash
# Run the notifier with secrets from ops/ntfy.env (never committed).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
[ -f ops/ntfy.env ] && set -a && . ops/ntfy.env && set +a
exec uv run --quiet --with numpy python src/watch.py "$@"
