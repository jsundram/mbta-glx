#!/bin/bash
# Is the whole thing actually running? One command, because the answer lives in
# four different places and none of them announce themselves when they stop.
#
# Run it from a Terminal on the capture host: `launchctl` needs the GUI domain,
# which an SSH or detached shell does not have.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ok() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad() { printf '  \033[31mBAD\033[0m  %s\n' "$1"; FAIL=1; }
FAIL=0

echo "agents"
for p in com.magoun.archiver com.magoun.daily com.magoun.server com.magoun.watch; do
  line=$(launchctl list 2>/dev/null | grep "$p$")
  pid=$(echo "$line" | awk '{print $1}')
  if [ -z "$line" ]; then bad "$p is not loaded"
  elif [ "$p" = com.magoun.daily ]; then ok "$p (scheduled; last exit $(echo "$line" | awk '{print $2}'))"
  elif [ "$pid" = "-" ]; then bad "$p is loaded but not running"
  else ok "$p (pid $pid)"; fi
done

echo "capture"
newest=$(ls -t data/live/rt-*.jsonl.gz 2>/dev/null | head -1)
if [ -z "$newest" ]; then bad "no archive file at all"
else
  age=$(( $(date +%s) - $(stat -f %m "$newest") ))
  # The archiver appends every 15 s; the largest ordinary gap measured over a
  # 15-hour day was 18 s. Ten minutes is the board's own threshold.
  [ "$age" -lt 600 ] && ok "last write ${age}s ago ($(basename "$newest"))" \
                     || bad "last write ${age}s ago -- losing data"
fi

echo "endpoints"
base=$(python3 -c "import json;print(json.load(open('data/config.json')).get('backend_url',''))")
# Read from server.py rather than typed here. These were two literal lists in three
# places, so a third route -- /today -- would have been added to the server, published
# through the proxy, and checked by nothing: a route that stops answering would then
# be exactly as visible as a route that never existed. Scraped, not imported, because
# this script must run with no dependencies installed.
exposed=$(awk '/^BROWSER_ROUTES = \{/{f=1;next} f&&/^\}/{f=0} f' src/server.py \
          | sed -n 's|.*"\(/[a-z]*\)".*|\1|p' | sort -u)
private=$(sed -n 's|.*u\.path == "\(/[a-z]*\)".*|\1|p' src/server.py | sort -u \
          | grep -vxF "$exposed" || true)
for p in $exposed; do
  code=$(curl -s -o /dev/null --max-time 6 -w '%{http_code}' "http://127.0.0.1:8723$p")
  [ "$code" = 200 ] && ok "localhost:8723$p" || bad "localhost:8723$p -> HTTP $code"
done
if [ -n "$base" ]; then
  for p in $exposed; do
    code=$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "$base$p")
    [ "$code" = 200 ] && ok "$base$p" || bad "$base$p -> HTTP $code (tailscale serve?)"
  done
  # The line that keeps the board static: only the allowlisted ones may be reachable.
  # These 404 because `/` on this hostname is mounted to another app (port 8770)
  # and that app does not have them -- not because Tailscale refuses an unmapped
  # path. Same verdict, different reason: if that app ever grew a /status, this
  # would go BAD without the board's backend having leaked anything.
  for p in $private; do
    code=$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "$base$p")
    [ "$code" = 404 ] && ok "$base$p is NOT exposed" \
                      || bad "$base$p answers $code -- it must not be on the tailnet"
  done
fi

echo "keys"
for f in ops/secrets.env ops/ntfy.env; do [ -f "$f" ] && . "$f" 2>/dev/null; done
[ -n "${MBTA_API_KEY:-}" ] && ok "MBTA_API_KEY set (backend is off the 20/min cap)" \
                           || bad "no MBTA_API_KEY; backend shares the anonymous cap"
[ -n "${MAGOUN_NTFY_TOPIC:-}" ] && ok "ntfy topics set" || bad "no ntfy topics"

echo
[ "$FAIL" = 0 ] && echo "all good" || echo "something is down -- see BAD above"
exit $FAIL
