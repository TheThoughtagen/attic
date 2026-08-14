#!/usr/bin/env bash
set -euo pipefail

# Resolved absolutely: the soak's `attic list` / `attic reap --dry-run` commands
# only work if `attic` is on PATH, and `cp launchd/...` below only works when run
# from the repo root. Neither can be assumed of the caller's cwd.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PLIST="$HOME/Library/LaunchAgents/com.attic.plist"
mkdir -p "$HOME/.attic/logs" "$HOME/Library/LaunchAgents"
cp "$REPO/launchd/com.attic.plist" "$PLIST"

uv tool install --editable "$REPO[tui]"

# Start paused. Reaping is enabled only after the soak in the README.
touch "$HOME/.attic/PAUSE"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "attic installed and PAUSED. Inventory is running; reaping is disabled."
echo "Remove $HOME/.attic/PAUSE to enable reaping."
