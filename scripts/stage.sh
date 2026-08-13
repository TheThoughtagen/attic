#!/usr/bin/env bash
# Stage attic against a real herdr pane, fully isolated from your live sessions.
#
# WHY THIS EXISTS
#   Every automated test in this repo runs against FakeHerdrClient. The live
#   integration tests are read-only. Nothing has ever watched attic close a real
#   pane, and nothing has confirmed `claude --resume <uuid>` actually brings a
#   session back. That is the promise the whole tool rests on, so verify it once
#   before granting attic authority over sessions you care about.
#
# ISOLATION
#   Everything happens in a herdr session named `attic-stage`, on its own socket
#   at ~/.config/herdr/sessions/attic-stage/herdr.sock. Your `default` session is
#   never contacted. attic's state goes to a scratch ATTIC_HOME, not ~/.attic.
#
# WHY YOU RUN STEP 1 AND NOT THE AGENT
#   herdr's server is a TUI and cannot spawn panes from a non-interactive
#   process (it fails with "ghostty error -2"). Creating the staging pane needs
#   a real terminal. Everything after that is scriptable.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="${STAGE:-$HOME/.attic-stage}"
SOCK="$HOME/.config/herdr/sessions/attic-stage/herdr.sock"
export ATTIC_HOME="$STAGE/attic"
export HERDR_SOCKET_PATH="$SOCK"

step="${1:-help}"

case "$step" in

start)
  # RUN THIS FROM A REAL TERMINAL. It opens a herdr TUI.
  mkdir -p "$STAGE/work" "$ATTIC_HOME"
  cd "$STAGE/work"
  git init -q 2>/dev/null || true
  printf 'print("staging demo")\n' > demo.py
  echo "Opening an isolated herdr session. Inside it:"
  echo "  1. start an agent:  claude"
  echo "  2. say something trivial so the session gets a UUID, e.g. 'hello'"
  echo "  3. leave it idle and detach (do NOT close the pane)"
  echo "  4. back here, run:  scripts/stage.sh verify"
  echo
  exec env -u HERDR_PANE_ID -u HERDR_TAB_ID -u HERDR_WORKSPACE_ID -u HERDR_ENV \
    HERDR_SOCKET_PATH="$SOCK" herdr --session attic-stage
  ;;

verify)
  echo "== isolation =="
  printf 'staging panes: '; herdr pane list | python3 -c "import sys,json;d=json.load(sys.stdin);ps=d['result']['panes'];print(len(ps),[p['pane_id'] for p in ps])"
  printf 'live panes:    '; HERDR_SOCKET_PATH="$HOME/.config/herdr/herdr.sock" herdr pane list | python3 -c "import sys,json;print(len(json.load(sys.stdin)['result']['panes']),'(must never be touched)')"

  echo
  echo "== agent panes attic can see =="
  herdr pane list | python3 -c "
import sys, json
for p in json.load(sys.stdin)['result']['panes']:
    if p.get('agent'):
        print(' ', p['pane_id'], p['agent_status'], (p.get('agent_session') or {}).get('value'))
"
  echo
  echo "== dry-run verdicts (nothing is closed) =="
  mkdir -p "$ATTIC_HOME"
  printf '{"idle_threshold_hours": 0.0006, "per_tick_cap": 1}\n' > "$ATTIC_HOME/config.json"
  uv run --project "$REPO" attic reap --dry-run || true
  echo
  echo "If a staging pane shows ARCHIVE, run:  scripts/stage.sh reap"
  ;;

reap)
  # THIS CLOSES A REAL PANE — the staging one, on the isolated socket.
  echo "== tick (archives + closes) =="
  uv run --project "$REPO" attic tick
  echo
  echo "== what was archived =="
  uv run --project "$REPO" attic list
  echo
  echo "== the pane should now be gone =="
  herdr pane list | python3 -c "import sys,json;print(len(json.load(sys.stdin)['result']['panes']),'panes remain')"
  echo
  echo "Now restore it:  scripts/stage.sh restore <archive-id>"
  ;;

restore)
  [ $# -ge 2 ] || { echo "usage: scripts/stage.sh restore <archive-id>"; exit 2; }
  uv run --project "$REPO" attic show "$2" | head -40
  echo
  echo "== restoring =="
  uv run --project "$REPO" attic restore "$2"
  echo
  echo "Attach and confirm the session came back WITH ITS HISTORY:"
  echo "  HERDR_SOCKET_PATH=$SOCK herdr session attach attic-stage"
  ;;

clean)
  herdr server stop 2>/dev/null || true
  sleep 1
  pkill -f 'herdr --session attic-stage' 2>/dev/null || true
  HERDR_SOCKET_PATH="$HOME/.config/herdr/herdr.sock" herdr session delete attic-stage 2>/dev/null || true
  rm -rf "$STAGE"
  echo "staging session and $STAGE removed; your default session untouched"
  ;;

*)
  cat <<'USAGE'
usage: scripts/stage.sh <start|verify|reap|restore <id>|clean>

  start    open an isolated herdr session (RUN FROM A REAL TERMINAL)
  verify   show isolation, agent panes, and dry-run verdicts — closes nothing
  reap     run a real tick: archive and CLOSE the staging pane
  restore  bring the archived session back and print how to attach
  clean    tear down the staging session and scratch state
USAGE
  ;;
esac
