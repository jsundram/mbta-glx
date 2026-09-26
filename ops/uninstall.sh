#!/bin/bash
set -uo pipefail
cd "$(dirname "$0")"
# Derived from the plists, not typed: install.sh gained com.magoun.server and this
# list did not, so `uninstall` left the backend running under KeepAlive with its
# plist still in place to come back at next login.
AGENTS=()
for f in com.magoun.*.plist; do AGENTS+=("${f%.plist}"); done
if [ "${1:-}" = "--list" ]; then printf '%s\n' "${AGENTS[@]}"; exit 0; fi

for p in "${AGENTS[@]}"; do
  launchctl bootout "gui/$(id -u)/$p" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$p.plist"
  echo "removed $p"
done
