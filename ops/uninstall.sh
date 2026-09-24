#!/bin/bash
set -uo pipefail
for p in com.magoun.archiver com.magoun.daily com.magoun.watch; do
  launchctl bootout "gui/$(id -u)/$p" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$p.plist"
  echo "removed $p"
done
