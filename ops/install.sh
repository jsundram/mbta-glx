#!/bin/bash
# Install the launchd agents. Run as yourself:  ./ops/install.sh
# NOT with sudo -- LaunchAgents live in your user's GUI domain and root has none.
set -uo pipefail

cd "$(dirname "$0")"
# The agents are whatever plists are here. Two hand-kept lists is how
# com.magoun.server ended up installed by one script and not removed by the
# other. `--list` exists so a test can compare both scripts to the directory.
AGENTS=()
for f in com.magoun.*.plist; do AGENTS+=("${f%.plist}"); done
if [ "${1:-}" = "--list" ]; then printf '%s\n' "${AGENTS[@]}"; exit 0; fi

UID_N="$(id -u)"
DOMAIN="gui/$UID_N"

if [ "$UID_N" -eq 0 ]; then
  cat >&2 <<'MSG'
error: do not run this with sudo.

LaunchAgents are per-user and load into your GUI domain (gui/501). Running as
root targets gui/0, which does not exist -- launchctl reports:
    Bootstrap failed: 125: Domain does not support specified action
Run it as yourself instead:   ./ops/install.sh
MSG
  exit 1
fi

if ! launchctl print "$DOMAIN" >/dev/null 2>&1; then
  echo "error: no GUI domain for uid $UID_N." >&2
  echo "You are probably in an SSH or detached shell. Run this from Terminal" >&2
  echo "while logged into the desktop session." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
fail=0
for p in "${AGENTS[@]}"; do
  if [ "$p" = com.magoun.watch ] && [ ! -f ntfy.env ]; then
    echo "skip  $p  (ops/ntfy.env not set up yet)"
    continue
  fi
  cp "$p.plist" "$HOME/Library/LaunchAgents/$p.plist"
  # bootout is ASYNCHRONOUS: bootstrapping before the unload completes fails with
  # "5: Input/output error", and leaves the agent stopped. Wait for it to go.
  launchctl bootout "$DOMAIN/$p" 2>/dev/null || true
  for _ in $(seq 1 50); do
    launchctl print "$DOMAIN/$p" >/dev/null 2>&1 || break
    sleep 0.2
  done
  if err=$(launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$p.plist" 2>&1); then
    echo "load  $p"
  else
    fail=1
    echo "FAIL  $p: ${err:-unknown}" >&2
    case "$err" in
      *"5: Input/output error"*)
        echo "      -> still unloading. Wait a few seconds and re-run." >&2 ;;
      *125*) echo "      -> wrong domain; are you running as root?" >&2 ;;
      *"No such file"*)
        echo "      -> plist references a missing path; check ops/*.plist." >&2 ;;
    esac
  fi
done

echo
echo "running agents:"
launchctl list | grep magoun || echo "  (none)"
exit $fail
