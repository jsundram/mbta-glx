#!/bin/bash
# Install the archiver and daily maintenance as launchd agents (survive reboot).
set -euo pipefail
cd "$(dirname "$0")"
for p in com.magoun.archiver com.magoun.daily com.magoun.watch; do
  cp "$p.plist" "$HOME/Library/LaunchAgents/$p.plist"
  launchctl bootout "gui/$UID/$p" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/$p.plist"
  echo "loaded $p"
done
launchctl list | grep magoun || true
