#!/usr/bin/env bash
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.you.attic.plist"
mkdir -p "$HOME/.attic/logs" "$HOME/Library/LaunchAgents"
cp launchd/com.you.attic.plist "$PLIST"

# Start paused. Reaping is enabled only after the soak in the README.
touch "$HOME/.attic/PAUSE"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "attic installed and PAUSED. Inventory is running; reaping is disabled."
echo "Remove $HOME/.attic/PAUSE to enable reaping."
