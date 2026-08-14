# Live verification — pin and snooze

Run 2026-08-13 against a **real** herdr agent pane, not `FakeHerdrClient`.

Every automated test for exemptions uses fakes. This project's central bug —
`attic` closing a session `claude --resume` could not recover — was invisible to
110 unit tests and surfaced only when a real pane was closed and restored. So the
exemption path gets the same treatment.

## Isolation

Headless server (`herdr server`, not `herdr --session X`, which launches the TUI
client) on its own socket, routed with `HERDR_SOCKET_PATH` — `HERDR_SESSION`
alone does **not** redirect the CLI and silently keeps talking to the default
socket.

Gate checked before any mutation: **staging 0 panes, live 12 panes.** Identical
counts would have meant aborting.

## Results

| # | Action | Output |
|---|---|---|
| 0 | baseline `tick` | `archived 0 pane(s); ok` |
| 1 | `reap --dry-run` | `skip w1:p2 (session transcript not written yet; claude --resume would fail)` |
| 2 | `pin w1:p2` | `pinned w1:p2 (term_658f5fcd867a82)` → dry-run: `skip … (pinned)` |
| 3 | `snooze w1:p2 4h` | dry-run: `skip … (snoozed until 2026-08-14T03:24:50Z)` |
| 4 | re-snooze **shorter** (`1h`) | `snoozed until …T00:24:51Z (was …T03:24:50Z)` |
| 5 | snooze while pinned | `note: pane is pinned; snooze applies only after unpin` |
| 6 | `unsnooze` | dry-run returns to the resumability reason |
| 7 | `state.json` keys | `['term_658f5fcd8255c1', 'term_658f5fcd867a82']` — terminal ids only |

Every requirement held:

- **Pane id resolved to a terminal id at command time** (step 2 prints both). A pin
  stored under `w1:p2` would protect the *slot*, and a new session opening there
  would silently inherit it.
- **Re-snoozing replaces and reports the previous deadline** (step 4). Shortening
  protection is visible rather than silent.
- **Snoozing a pinned pane says so** (step 5) instead of implying protection changed.
- **Exemption reasons appear in `reap --dry-run`** — the same output the operator
  reads during the soak.

## An unplanned finding: the resumability gate reproduced live

The staging agent completed a full prompt/response exchange and was reported
`idle` with a genuine session UUID (`8faf3667-…`), yet **no transcript existed on
disk** 30+ seconds later — `~/.claude/projects/-Users-you--attic-exempt-work/`
was never created.

That is the exact bug the gate was added for, reproduced independently: a session
can be idle, carry a real UUID, and be entirely unrecoverable. Without the gate,
`attic` would have archived and closed it, reported success, and handed back an
empty prompt on restore.

It also means step 6's dry-run correctly shows the gate blocking rather than
`ARCHIVE` — the gate outranking an expired snooze is the right precedence.

## Minor issue found

Snooze deadlines render with microseconds (`2026-08-14T03:24:50.949784Z`). Correct
but ugly for a user-facing timestamp; worth truncating to seconds.

## Teardown

Staging session stopped and deleted, `~/.attic-exempt` removed, `herdr session
list` back to `default` only. The live server was never contacted on any step.
