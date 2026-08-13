# attic — herdr agent archiver

**Date:** 2026-08-13
**Status:** Approved design, pending implementation plan
**Author:** you (with Claude)

## Problem

Two symptoms with one root cause.

**Resource leakage.** Long-lived `claude` CLI processes accumulate on the Mac and are
never reclaimed. After one to two weeks of uptime the machine is bottlenecked. Measured
at design time, on only 12 hours of uptime: load average 16.4, six live Claude agents
holding roughly 1.8 GB RSS, each with an oh-my-claudecode node sidecar of about 37 MB,
plus a graveyard of six `EXITED` zellij sessions dating back a month.

**State loss.** When the multiplexer closes, two things disappear that matter:

1. **The inventory** — which repos, which task, which pane. The work can be resumed;
   knowing what the nine panes were *for* cannot be reconstructed.
2. **Scrollback** — build output, error traces, the agent's last responses that had not
   yet been acted on.

Explicitly *not* lost and therefore *not* in scope: agent conversation context. Claude
Code already persists transcripts and resumes them by session UUID.

The common cause is that processes are tracked by the terminal that spawned them. When
the terminal dies the accounting dies with it: the resources survive, the knowledge of
them does not.

## Scope

**In scope:** local Claude/agent CLI processes managed by herdr on this Mac.

**Out of scope**, confirmed with the user during design:

- Remote sandboxes and VMs (exe.dev, Daytona)
- Claude Code cloud sessions, scheduled routines, background cloud agents
- Network tunnels and MCP connection churn
- Layout and pane-arrangement restoration
- Agent conversation context (already solved by Claude Code)

## Constraints

- **Archive before kill.** Nothing is terminated until its scrollback and resume handle
  are durable on disk. This was the user's explicit condition for granting auto-kill
  authority.
- **Auto-kill idle only.** The reaper acts without prompting, but only on provably idle
  agents.
- **No system pip.** Per the user's global CLAUDE.md, Python dependencies go through
  `uv`, never the system interpreter.

## Approach

Chosen: **herdr-native**. herdr 0.8.0 (protocol 19) is already running as a persistent
server on `~/.config/herdr/herdr.sock`, and all six of the user's Claude agents are
herdr-managed panes. herdr exposes exactly the primitives needed:

| Need | herdr primitive |
|---|---|
| Inventory | `herdr api snapshot`, `herdr pane list` |
| Scrollback | `herdr pane read <id> --source recent-unwrapped --lines N --format text` |
| Reap | `herdr pane close <id>` |
| Version guard | `herdr status` (reports protocol) |

Each agent pane carries an `agent_session.value` UUID, which is a Claude Code session ID.
`claude --resume <uuid>` reconstitutes the exact conversation, so recovery is lossless.
Each pane also reports `scroll.max_offset_from_bottom`, which sizes the scrollback read
per pane rather than relying on a fixed line count.

### Approaches rejected

**Process-table reaper** (multiplexer-agnostic `ps` scan correlated to
`~/.claude/projects/` transcripts). Rejected: it structurally cannot capture scrollback,
because there is no pane buffer to read outside the multiplexer. That fails one of the
two stated requirements outright. Its idle heuristic (CPU activity) is also a guess where
herdr provides an explicit answer.

**Hybrid** (herdr-native plus a `ps` backstop that reports non-herdr strays). Rejected as
YAGNI: all six agents are currently herdr-managed and the user is consolidating onto
herdr, so the backstop would find nothing. Revisit only if agents start appearing in bare
terminal tabs.

## Architecture

One script, one LaunchAgent, three verbs beyond `tick`.

`attic tick` runs every 5 minutes and performs **snapshot**, then **reap**, in that order.

**Snapshot** runs unconditionally, even when reaping is paused. It calls
`herdr api snapshot` and appends one compact line to
`~/.attic/inventory/YYYY-MM-DD.jsonl` containing the timestamp and, for every pane:
workspace label, cwd, task title, agent status, and session UUID. This alone answers
"what was open" for any past moment, whether or not anything was reaped.

**Reap** archives-then-closes qualifying panes. Per pane: drain scrollback to
`~/.attic/archive/<ts>-<slug>/scrollback.txt`, write `manifest.json` beside it, fsync
both, then `herdr pane close`. If the read fails or returns empty, the pane is left alone
and the failure is logged.

Snapshot and reap are deliberately decoupled. Snapshot is pure observation and cannot
cause harm, so it is ungated. Every safety guard, kill switch, and version check gates
only the reap half — the most-relied-upon feature has the fewest ways to fail.

### Components

| Component | Responsibility | Depends on |
|---|---|---|
| `HerdrClient` | The only code that talks to herdr: `snapshot()`, `pane_list()`, `pane_read()`, `pane_close()`, `protocol()` | herdr CLI |
| `decide()` | Pure policy function: `(panes, state, now, config) -> [Action]` | nothing |
| `Archiver` | Writes archive dirs, fsyncs, appends to index | filesystem |
| `Inventory` | Appends snapshot lines, prunes old files | filesystem |
| `CLI` | `tick`, `list`, `show`, `restore`, `reap --dry-run`, `prune` | all of the above |

`decide()` has no I/O, no clock, and no herdr dependency. It receives the pane list and
idle-state map and returns `Archive(pane)` or `Skip(pane, reason)` actions. This is where
all policy lives and where all policy is tested.

The two herdr read calls have distinct roles and must not be conflated: `pane_list()` is
the **authoritative input to policy**, because it carries the per-pane `agent_status`,
`revision`, `focused`, and `scroll` fields that `decide()` requires. `snapshot()` is used
**only** to write the inventory line. If the two ever disagree, `pane_list()` wins for
reaping decisions.

### Data model

Runtime data lives in `~/.attic/`, deliberately outside any git tree, because archives
contain full scrollback from client repositories. Code lives in `~/repos/attic/`.

```
~/.attic/
  config.json            # idle_threshold_hours, per_tick_cap, retention
  state.json             # terminal_id -> {first_idle_at, last_revision}
  PAUSE                  # presence disables reaping
  inventory/YYYY-MM-DD.jsonl
  archive/
    index.jsonl          # append-only log of archived sessions
    <ts>-<slug>/
      manifest.json
      scrollback.txt
  logs/attic.log
```

**Archive identity.** An archive's directory name is its ID, formed as
`<UTC ts>-<slug>` where the timestamp is `YYYYMMDDTHHMMSSZ` and the slug is the pane's
`terminal_title_stripped` lowercased, non-alphanumerics collapsed to single hyphens, and
truncated to 48 characters — e.g.
`20260813T154700Z-debug-batch-transaction-group-logging-in-pro`. If that name already
exists, a `-2`, `-3`, ... suffix is appended. This ID is what `attic show <id>` and
`attic restore <id>` take, and a unique prefix of it is accepted as shorthand.

**State is keyed by `terminal_id`** (e.g. `term_658ed00535c1118`), never by `pane_id`
(e.g. `w4:p2`). Pane IDs are positional and get recycled when panes close and reopen.
Keying idle timers by pane ID would let a brand-new pane inherit a dead one's idle clock
and be archived moments after it opens. Terminal IDs are unique per terminal instance.

**Manifest** is the recovery contract:

```json
{
  "pane_id": "w4:p2",
  "terminal_id": "term_658ed00535c1118",
  "workspace": "wh dev",
  "session_uuid": "55555555-5555-4555-8555-555555555555",
  "agent": "claude",
  "cwd": "/Users/you/data/projects/analytics",
  "title": "Debug batch transaction group logging in production",
  "idle_since": "2026-08-13T05:12:00-06:00",
  "archived_at": "2026-08-13T09:47:00-06:00",
  "scrollback_lines": 1818,
  "resume": "cd /Users/you/data/projects/analytics && claude --resume 55555555-5555-4555-8555-555555555555"
}
```

The resume command is stored as a literal string rather than reconstructed at restore
time. If Claude Code changes its resume flags later, old archives still record exactly
what worked when they were written; the archive is self-describing.

### Restore behavior

`attic restore <id>` reads the manifest and opens the session in a **new tab in the
currently focused workspace**, not in the pane's original position. Recreating the
original layout is explicitly out of scope, and the original workspace may no longer
exist. The tab runs the manifest's stored `resume` string verbatim.

Restore is **non-destructive and repeatable**: the archive directory is retained after a
restore, and `index.jsonl` gains a `restored_at` timestamp rather than losing the entry.
Restoring twice yields two panes, which is a harmless outcome and avoids a
delete-on-restore path that could lose an archive if the restore itself fails.

If the manifest's `cwd` no longer exists, restore aborts with the manifest printed and
does not open a pane.

### Implementation language

A single-file Python script, stdlib-only, with a PEP 723 inline metadata header, invoked
via `uv run --script`. This satisfies the no-system-pip constraint without requiring a
venv. herdr emits JSON, which makes bash plus jq the fragile choice.

## Reap policy

A pane is archived only if **all five** hold:

1. `agent_status == "idle"` — not `working`, not `blocked`, not `unknown`
2. It has an `agent_session.value` UUID
3. `focused == false`
4. Its `revision` counter has not changed for the full idle window
5. Idle duration >= threshold (default **4 hours**)

Rules 1 and 4 are intentionally redundant. `agent_status` is herdr's interpretation;
`revision` is raw output activity. Requiring both means a status-detection bug alone
cannot cost a session.

**`blocked` is never reaped, at any age.** A blocked agent is parked on a permission
prompt: it consumes no CPU and represents a decision the user has not yet made. If
blocked panes accumulate, that is a notification problem, not a reaping one.

**Panes with no `agent` key are never candidates** — plain shells, `nvim`, and similar.

**Rule 2 exists because a session without a UUID has no recovery path.** Unrecoverable
means untouchable.

## Safety guards

- **Kill switch.** If `~/.attic/PAUSE` exists, `tick` snapshots and exits. A single
  `touch` disables all reaping without unloading the LaunchAgent.
- **Protocol guard.** `tick` reads `herdr status` and compares protocol against a pinned
  value (19 at design time). On mismatch it snapshots, logs loudly, and refuses to reap.
  This is the mitigation for the herdr-API-churn risk accepted when choosing this
  approach: a herdr upgrade degrades the system to inventory-only rather than letting
  mis-parsed JSON decide what to kill.
- **Dry-run is first-class.** `attic reap --dry-run` prints the verdict and reasoning for
  every pane. This is how trust is established before the timer is enabled, and how the
  system is debugged afterward.
- **Per-tick cap.** At most **3** panes archived per tick. This is blast-radius control,
  not performance: it converts a catastrophic bug into a noticeable one.

## Error handling

Every failure mode resolves toward doing nothing.

| Failure | Behavior |
|---|---|
| herdr socket down or CLI missing | Log, exit 0. Never crash the LaunchAgent loop. |
| Malformed JSON from herdr | Log raw output, skip the tick entirely. |
| `pane read` fails or returns empty | Skip that pane, do not close, log. Retry next tick. |
| Archive write or fsync fails | Skip that pane, do not close. |
| `pane close` fails after successful archive | Archive kept and marked `close_failed: true`; pane stays alive. Worst case is a duplicate archive, never a lost session. |

`tick` always exits 0. A reaper that crashes its own timer is a reaper that silently
stops protecting the user.

Every error path fails toward leaving the pane running. This asymmetry is deliberate: a
missed reap costs memory that would have leaked anyway, while a bad kill costs work. The
system always prefers the failure mode the user already lives with.

## Retention

Archives are pruned after **30 days**, inventory JSONL after **90**, both by `attic prune`
running inside `tick`. Pruning is the only destructive operation with no undo, so each
deletion is logged with its manifest title.

## Testing

All herdr interaction is confined to `HerdrClient`, so the dangerous paths are testable
by injecting a fake. Built test-first per `superpowers:test-driven-development`.

Unit tests run `decide()` against fixture JSON captured from the user's real
`herdr pane list` output, preserved at
`docs/superpowers/specs/pane-list-sample.json`. Grounding the fixture in live output
encodes herdr's actual quirks: `unknown` status on nvim panes, the missing `agent` key on
bare shells, and `cwd` disagreeing with `foreground_cwd` on case (`clients` vs
`clients` in pane `w3:p1`), which would break naive path matching.

| Case | Expected |
|---|---|
| `working` pane, idle 10h | Skip — status |
| `blocked` pane, idle 10h | Skip — blocked never reaped |
| `focused` idle pane past threshold | Skip — focused |
| Idle pane with no session UUID | Skip — unrecoverable |
| `revision` bumped mid-window | Clock resets, skip |
| Five eligible panes | Exactly 3 archived (per-tick cap) |
| `PAUSE` file present | Zero actions, snapshot still written |
| Protocol != 19 | Zero actions, warning logged |
| New pane reuses pane ID `w4:p2` | Fresh idle clock (terminal_id keying) |

**Durability tests** use a fake client that fails `pane_read` on demand: assert
`pane_close` is never called, and that a partial archive directory produces no
`index.jsonl` entry. This proves the property the user actually asked for, and it is only
provable against a fake — a real herdr cannot be made to fail on command.

**One integration test** against real herdr: spawn a throwaway pane, point `ATTIC_HOME`
at a temp directory, set the threshold to 0, and assert the full round trip — archive
written, pane closed, `attic restore` yields a resumable session. Slow; excluded from the
default run.

## One-time cleanup

Chores that the tool should not own, because they will not recur once the user is fully
on herdr. Tracked separately from implementation.

1. **Six dead zellij sessions** (`dev`, `sidemoney`, `adventurous-xylophone`,
   `hopeful-iguanadon`, `jumping-apricot`, `undulating-horse`), aged 9 days to 1 month 7
   days, all `EXITED`. `~/.cache/zellij` is empty, so resurrection data is elsewhere or
   already gone. Locate it, copy anything real to `~/.attic/legacy/`, then
   `zellij delete-session` each. Nothing is deleted before it is copied.
2. **Three archived Daytona sandboxes** (79, 133, 228 days old). Archived means no
   compute, but storage may still bill. The local CLI is v0.154 against a v0.204 API, so
   this needs `brew upgrade` first. Deliberately kept separate: different system,
   different risks, and outside the leak the user identified as painful.
3. **Habit change:** launch agents through herdr only. This closes the coverage gap that
   made the hybrid approach tempting. No tooling required.

## Success criteria

- After two weeks of uptime, idle Claude agents do not accumulate; load average reflects
  active work only.
- Every archived session is restorable to a working Claude Code conversation via
  `attic restore`.
- `attic list` answers "what was I working on" for any archived session within the
  retention window.
- Inventory answers "what was open" for any timestamp within 90 days.
- No `working`, `blocked`, or focused pane is ever archived.
