#!/usr/bin/env bash
set -euo pipefail

# Resolved absolutely: the soak's `attic list` / `attic reap --dry-run` commands
# only work if `attic` is on PATH, and `cp launchd/...` below only works when run
# from the repo root. Neither can be assumed of the caller's cwd.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PLIST="$HOME/Library/LaunchAgents/com.attic.plist"
mkdir -p "$HOME/.attic/logs" "$HOME/Library/LaunchAgents"

uv tool install --editable "$REPO[tui]"

# Resolve the real paths NOW, on this machine. The plist in launchd/ is a
# template full of __TOKENS__; copying it verbatim yields an agent that loads
# and then fails silently every five minutes.
ATTIC_BIN="$(command -v attic || true)"
if [ -z "$ATTIC_BIN" ]; then
  echo "attic is not on PATH after install — is ~/.local/bin in your PATH?" >&2
  exit 1
fi

# launchd starts with no PATH at all, and attic shells out to herdr. Put herdr's
# own directory first rather than guessing /opt/homebrew/bin, which is wrong on
# Intel macs and on Linux.
HERDR_BIN="$(command -v herdr || true)"
if [ -z "$HERDR_BIN" ]; then
  echo "warning: herdr not found on PATH; every tick will log 'herdr unavailable'" >&2
  HERDR_DIR="/opt/homebrew/bin"
else
  HERDR_DIR="$(dirname "$HERDR_BIN")"
fi
AGENT_PATH="$HERDR_DIR:$(dirname "$ATTIC_BIN"):/usr/local/bin:/usr/bin:/bin"

sed -e "s|__ATTIC_BIN__|$ATTIC_BIN|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__PATH__|$AGENT_PATH|g" \
    "$REPO/launchd/com.attic.plist" > "$PLIST"

if grep -q '__' "$PLIST"; then
  echo "plist still contains unsubstituted tokens; refusing to install" >&2
  grep -n '__' "$PLIST" >&2
  exit 1
fi

# Start paused. Reaping is enabled only after the soak in the README.
touch "$HOME/.attic/PAUSE"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

# Prove it actually runs here rather than assuming: launchd's environment is not
# your shell's, and a tick that dies under launchd dies silently.
launchctl start com.attic
sleep 3
if [ -s "$HOME/.attic/logs/launchd.err.log" ] &&
   grep -qiE 'traceback|command not found|no such file' "$HOME/.attic/logs/launchd.err.log"; then
  echo "the first tick failed under launchd — see $HOME/.attic/logs/launchd.err.log" >&2
  tail -5 "$HOME/.attic/logs/launchd.err.log" >&2
  exit 1
fi

echo "attic installed and PAUSED. Inventory is running; reaping is disabled."
echo "Remove $HOME/.attic/PAUSE to enable reaping."
