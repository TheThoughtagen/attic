# attic

Archives idle herdr agent panes before reclaiming them, and keeps a running inventory of
what was open.

See the design spec: `docs/superpowers/specs/2026-08-13-attic-herdr-agent-archiver-design.md`

## Install

```bash
./install.sh          # installs the LaunchAgent, starts PAUSED, puts `attic` on PATH via `uv tool install`
```

## The soak — do this before enabling reaping

`attic` installs paused. Inventory runs from minute one; reaping is off.

1. Let inventory run for a few days: `attic list` stays empty, but
   `~/.attic/inventory/` fills up. Confirm it is capturing what you expect.
2. Run `attic reap --dry-run` daily. Read every verdict. Confirm that nothing you
   care about is ever marked `ARCHIVE`, especially anything `blocked`. Dry-run works
   while paused and reports real idle durations — the clock keeps advancing during a
   pause, so what you see is what would actually happen.
3. When the verdicts look right, `rm ~/.attic/PAUSE`. Expect the first unpaused tick
   to act immediately on panes the dry-run has been showing as `ARCHIVE` — they have
   been genuinely idle the whole time, and the per-tick cap of 3 bounds the burst.
4. After the first real archive, run `attic restore <id>` immediately and confirm the
   session resumes with its history intact.

## Commands

| Command | Effect |
|---|---|
| `attic tick` | Snapshot inventory, then reap if all guards pass (what launchd runs) |
| `attic reap --dry-run` | Print a verdict and reason for every pane; change nothing |
| `attic list` | List archived sessions, newest first |
| `attic show <id>` | Print an archive's manifest and scrollback (unique prefix works) |
| `attic restore <id>` | Reopen the session in a new tab; archive is kept |

## Pausing

```bash
touch ~/.attic/PAUSE     # inventory continues, reaping stops
rm ~/.attic/PAUSE        # reaping resumes
```

## What gets archived

Only panes that are **all** of: an agent pane, `agent_status == idle`, holding a session
UUID, unfocused, with an unchanged revision counter, idle for 4+ hours. At most 3 per tick.

`blocked` panes are never archived at any age — they are waiting on you.

## What scrollback actually contains

`herdr pane read` returns the pane's **rendered terminal frames**, including TUI chrome
(status line, box borders, spinners). It is a faithful record of what was on screen, not
a clean transcript. The conversation itself is recovered by `claude --resume`, which is
what the manifest's `resume` command does.

## `attic ui` — the control surface

```bash
attic ui        # needs the tui extra installed (see below)
```

A three-tab Textual dashboard — Fleet, Activity, Attic — read from the same evaluation
pipeline as `attic tick`, so what the UI shows and what the reaper will do can't diverge.

**Vim motions** (never mutate anything — they only move the cursor or switch tabs):

| Key | Effect |
|---|---|
| `j` / `k` | cursor down / up |
| `ctrl+d` / `ctrl+u` | half page down / up |
| `ctrl+f` / `ctrl+b` | page down / up |
| `gg` / `G` | jump to top / bottom |
| `gt` / `gT` | next / previous tab |
| `1gt`, `2gt`, `3gt` | jump to a specific tab |
| `R` | force an immediate refresh |
| `q` | quit |

**Mutations require typing a `:` command** — pinning, snoozing, archiving, and restoring
never happen on a bare keystroke, deliberately, the same way vim needs `:` before anything
destructive:

| Command | Tab | Effect |
|---|---|---|
| `:pin` | Fleet | never reap the selected pane |
| `:unpin` | Fleet | allow the selected pane to be reaped again |
| `:snooze <duration>` | Fleet | protect the selected pane until a deadline, e.g. `:snooze 4h` |
| `:unsnooze` | Fleet | clear the selected pane's snooze |
| `:archive` | Fleet | archive and close the selected pane now, skipping the idle threshold |
| `:restore` | Attic | reopen the selected archived session |
| `:help` | any | list available commands |
| `:q` / `:quit` | any | quit |

The command line captures its target — the selected row — the moment it opens, not when
you press Enter, so the 2-second background refresh can never retarget a command you're
still typing.

### As a herdr plugin

`herdr-plugin.toml` exposes `attic ui` as a pane (`control`) and adds `pin`/`snooze`
context actions on any pane, so pinning or snoozing a session doesn't require switching
to the attic tab at all. Install by linking the plugin per herdr's plugin docs; every
command it runs goes through `uv run --extra tui --project .`, so it needs no separate
environment setup beyond what `./install.sh` or `uv sync --extra tui` already provides.

## Configuration

`~/.attic/config.json`, all keys optional:

```json
{
  "idle_threshold_hours": 4.0,
  "per_tick_cap": 3,
  "archive_retention_days": 30,
  "inventory_retention_days": 90,
  "herdr_protocol": 19,
  "quiet_hours": "22:00-08:00"
}
```

### `quiet_hours`

An overnight window, in your machine's **local** time, during which nothing is
archived and the idle clock is continuously reset. Omit it (or set `null`) to
disable — that is the default.

Without it, a session you leave open at bedtime passes the idle threshold while
you sleep and is archived by the first tick of the morning. With it, the clock
restarts when the window ends, so a session needs a full threshold of *waking*
time before it becomes eligible:

```text
21:00  goes idle
22:00  window opens   → clock re-stamped every tick, nothing reaped
08:00  window closes  → clock reads ~5 minutes old
11:55  4h of waking idleness elapsed → archived
```

Windows that cross midnight are the normal case; same-day windows
(`"01:00-05:00"`) work too. The start is inclusive and the end exclusive, so
`08:00` is already the working day. Daylight-saving transitions are handled by
the system timezone rather than by arithmetic.

`attic reap --dry-run` reports the resolved zone in the skip reason
(`overnight hours (22:00-08:00 America/Chicago)`) — check it once, since a
daemon that resolves a different zone than your shell would shift the window
by hours. A malformed value aborts the tick and archives nothing.

## Development

```bash
uv run pytest                  # unit tests
uv run pytest -m integration   # against the live herdr server
```
