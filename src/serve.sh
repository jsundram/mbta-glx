#!/bin/bash
# Run the backend with secrets from ops/ (never committed).
#
# The only reason this exists rather than the plist invoking uv directly: the
# plist cannot carry MBTA_API_KEY without committing it. service.py reads the key
# from the environment and sends it as x-api-key, which lifts this process off
# the 20 requests/minute anonymous cap -- shared per IP, and one open board is
# most of it.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
# secrets.env is the name now that this holds more than the ntfy topics; ntfy.env
# is still read so an existing install keeps working without being touched.
for f in ops/ntfy.env ops/secrets.env; do
  [ -f "$f" ] && set -a && . "$f" && set +a
done
exec uv run --quiet --with polars --with gtfs-realtime-bindings \
    python src/server.py "${1:-8723}"
