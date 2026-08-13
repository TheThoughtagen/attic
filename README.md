# attic

Archives idle herdr agent panes before reclaiming them, and keeps a running inventory of
what was open.

See the design spec: `docs/superpowers/specs/2026-08-13-attic-herdr-agent-archiver-design.md`

## Install

```bash
./install.sh          # installs the LaunchAgent, starts PAUSED
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

## Configuration

`~/.attic/config.json`, all keys optional:

```json
{
  "idle_threshold_hours": 4.0,
  "per_tick_cap": 3,
  "archive_retention_days": 30,
  "inventory_retention_days": 90,
  "herdr_protocol": 19
}
```

## Development

```bash
uv run pytest                  # unit tests
uv run pytest -m integration   # against the live herdr server
```
