#!/bin/bash
# Run the notifier with secrets from ops/ntfy.env (never committed).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
for f in ops/ntfy.env ops/secrets.env; do
  [ -f "$f" ] && set -a && . "$f" && set +a
done

# The notifier has to fire at a wall-clock instant, and a sleeping host runs the
# missed tick on wake instead -- far too late to leave for a train. caffeinate held
# that off only for as long as somebody remembered to run it; here it is part of
# the job, so it comes back with the launchd agent after a reboot.
#
# It does NOT survive a lid close, and nothing on this host does. watch.py checks
# whether the walk still fits before sending a late leave-now, so the gap is a
# missed nudge rather than a wrong one. Closing it for real means moving off this
# Mac -- architecture.md, section 5.
#
# Guarded rather than assumed: caffeinate is macOS-only and this file should still
# run wherever the notifier moves next.
CAF=""
command -v caffeinate >/dev/null 2>&1 && CAF="caffeinate -i -s"
# The agents' own environment, not `uv run` -- ops/venv.sh says why.
# gtfs-realtime-bindings is in it and is not optional. service.skipped_trips
# imports google.transit inside its try, so without it every skip lookup raises
# and is swallowed as though cdn.mbta.com were down -- the notifier then never
# sees a SKIPPED marker, never takes the act-at-once path, and waits out the 240 s
# debounce on a train MBTA has already said is not stopping. Silent, and it was
# silent from the first day this agent ran.
. ops/venv.sh
[ -x "$PY" ] || { echo "no $PY -- run ./ops/install.sh" >&2; exit 1; }
exec $CAF "$PY" src/watch.py "$@"
