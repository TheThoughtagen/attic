# Live verification — herdr plugin manifest

Run 2026-08-14 against an isolated herdr 0.8.0 session. The spec flagged two things
as unverified and forbade assuming them; both were probed with a throwaway manifest
that printed its own environment.

## Isolation and restoration

herdr's plugin registry is **global**, not session-scoped (`.plugins.lock` lives at
`~/.config/herdr/`, not under `sessions/`). The registry was empty beforehand, so the
probe snapshotted it, linked, probed, unlinked, and verified an exact restore:

```
before: No plugins installed.
after : No plugins installed.
```

Panes ran on a separate socket (`sessions/attic-plugin/herdr.sock`); the isolation
gate confirmed staging 0 panes vs live 12 before anything was linked.

## Unknown 1 — does `--project .` resolve?

**Yes.** The pane entrypoint runs with the plugin root as its working directory:

```
PWD=/Users/you/repos/attic
```

`HERDR_PLUGIN_ROOT=/Users/you/repos/attic` is also injected, and would be the
more robust choice if that cwd behaviour ever changes.

## Unknown 2 — how does an action learn which pane it was invoked on?

**Not through argv.** The probe printed `ARGV=` — empty. `herdr plugin action invoke`
has no `--pane` flag; the target arrives only in the injected environment:

```
HERDR_PLUGIN_CONTEXT_JSON={"workspace_id":"w1","workspace_label":"plugin-probe",
  "workspace_cwd":"...","tab_id":"w1:t2","tab_label":"2",
  "focused_pane_id":"w1:p2","focused_pane_cwd":"...",
  "focused_pane_status":"unknown","invocation_source":"cli",
  "correlation_id":"cli:plugin"}
```

The manifest therefore parses `focused_pane_id` out of that JSON. **The obvious guess
— `attic pin $HERDR_PANE_ID` — would have been wrong**: `HERDR_PANE_ID` is the
plugin's own pane, not the pane the user acted on, so the action would have pinned
the wrong session. This is the same class of mistake as the `tab_create` response
shape that would have broken every restore in this project while all tests passed.

## Full injected environment

Present for both pane and action entrypoints:

```
HERDR_BIN_PATH          /opt/homebrew/bin/herdr
HERDR_ENV               1
HERDR_PANE_ID           the plugin's own pane
HERDR_PLUGIN_CONFIG_DIR ~/.config/herdr/plugins/config/attic
HERDR_PLUGIN_CONTEXT_JSON  (above)
HERDR_PLUGIN_ID         attic
HERDR_PLUGIN_ROOT       the linked directory
HERDR_PLUGIN_STATE_DIR  ~/.local/state/herdr/plugins/attic
HERDR_SESSION           the session name
HERDR_SOCKET_PATH       that session's socket
HERDR_TAB_ID / HERDR_WORKSPACE_ID
```

Actions additionally get `HERDR_PLUGIN_ACTION_ID`; panes get
`HERDR_PLUGIN_ENTRYPOINT_ID`.

`HERDR_SOCKET_PATH` matters most: it makes `attic ui` automatically target whichever
session launched it. During earlier staging this had to be arranged by hand, and
getting it wrong silently pointed a test at the live server.

## CLI shapes confirmed

```
herdr plugin link <dir>
herdr plugin pane open --plugin <id> --entrypoint <id> [--placement ...]
herdr plugin action invoke <action_id> --plugin <id>     # no --pane flag
herdr plugin unlink <id>
```
