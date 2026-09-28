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
pub=$(sed -n 's|^PUBLIC_PORT = \([0-9]*\).*|\1|p' src/server.py)
for p in $exposed; do
  code=$(curl -s -o /dev/null --max-time 6 -w '%{http_code}' "http://127.0.0.1:$pub$p")
  [ "$code" = 200 ] && ok "localhost:$pub$p" || bad "localhost:$pub$p -> HTTP $code"
done
# The split, checked from the inside. Each private route must be alive on the
# private port and absent from the public one -- that is the property, and it is now
# a property of this process rather than of which paths somebody remembered to mount.
for p in $private; do
  [ "$p" = "/api" ] && continue   # it calls MBTA; costs a request and can 503 honestly
  priv=$(curl -s -o /dev/null --max-time 6 -w '%{http_code}' "http://127.0.0.1:8723$p")
  publ=$(curl -s -o /dev/null --max-time 6 -w '%{http_code}' "http://127.0.0.1:$pub$p")
  [ "$priv" = 200 ] && [ "$publ" = 404 ] \
    && ok "$p is private (8723 $priv, $pub $publ)" \
    || bad "$p: 8723 says $priv, public port says $publ -- the split is not holding"
done
if [ -n "$base" ]; then
  for p in $exposed; do
    code=$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "$base$p")
    [ "$code" = 200 ] && ok "$base$p" || bad "$base$p -> HTTP $code (tailscale serve?)"
  done
  # And from the outside. These 404 because the public port does not have them, not
  # because some other app on that hostname happens not to -- which is what this
  # checked before the board's origin moved onto a port of its own.
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
