# `attic ui` — control surface for the archiver

**Date:** 2026-08-13
**Status:** Approved design, pending implementation plan
**Author:** you (with Claude)
**Builds on:** `2026-08-13-attic-herdr-agent-archiver-design.md`

## Problem

`attic` works — it archives idle herdr agent panes and restores them — but it is
entirely non-interactive. Everything it does is visible only through
`attic reap --dry-run`, `attic list`, and a log file. Four questions have no good
answer today:

1. **What did it do, and was it right?** Archives land in `index.jsonl`, but
   *skips are persisted nowhere*. "Why didn't it reap that pane last Tuesday?"
   is currently unanswerable.
2. **What is it about to do?** `reap --dry-run` answers this, but as a one-shot
   text dump rather than something you can watch.
3. **Where did that session go, and how do I get it back?** `attic list` plus
   `attic show <id>` plus `attic restore <id>` — three commands and a copied ID.
4. **How do I protect a session I must not lose?** There is no answer. Policy is
   a pure function of idle time; nothing can be exempted.

## Scope

**In:** a Textual TUI (`attic ui`) with three views; snooze/pin exemptions in the
reap policy; per-pane verdicts recorded in the inventory; a herdr plugin manifest.

**Out:** ranges and counts in the command line (`:1,5snooze`); push-based updates
via herdr's `events.subscribe`; any change to the reaping schedule itself.

## Constraints

- **One source of truth.** The TUI must not reimplement policy. This project has
  been bitten three times by two implementations of one truth disagreeing — most
  sharply when a guessed `tab_create` response shape passed every test because
  the fake encoded the same wrong assumption.
- **The daemon path stays hermetic.** `attic tick` runs under launchd with no
  `LANG`, no `PATH`, and no visible stderr. It must keep importing nothing but
  stdlib, so a broken TUI dependency can never stop reaping.
- **Protection can only add safety.** No TUI action may cause a pane to be closed
  that the reaper would have spared, except the explicit `:archive` command — and
  even that cannot bypass the resumability gate.

## Architecture

Three layers, with policy owned by exactly one of them.

```
herdr  ──plugin manifest──▶  attic ui  (Textual)
                                │ in-process calls
                                ▼
              policy.decide / resumable / catalog / restore
                                │
                                ▼
                    ~/.attic  +  herdr pane list
```

The TUI imports and calls `decide()`, `resume_blocker()`, `load_manifests()`
**in-process**. There is no serialization boundary and no second implementation:
what the Fleet view previews is not a faithful rendering of what the reaper will
do, it is the same function call. Drift is structurally impossible rather than
contract-managed.

### Why Python rather than Rust

herdr is Rust + ratatui and a Rust TUI was considered. The herdr plugin system
does not constrain the choice — per its documentation, a manifest "could launch
Bash, PowerShell, Python, Rust, Go, Lua, Bun, or any other command available on
the user's machine", and pane entrypoints are argv arrays. Three things then
favour Python:

- **In-process policy access**, as above. Rust would require either a JSON
  contract (safe but a wire where none is needed) or a reimplementation (the
  failure this project keeps hitting).
- **`herdr plugin link` registers a working directory without running build
  commands.** Python needs no build step; a Rust plugin would need a `[[build]]`
  entry invoking cargo before herdr registers it.
- **One language, one install** in a 962-line project.

### Dependency isolation

Textual is declared in an optional `[project.optional-dependencies] tui` group.
Nothing on the `attic tick` path imports it. The hermetic guarantee that keeps
the LaunchAgent alive is unaffected.

### herdr plugin packaging

A `herdr-plugin.toml` at the repo root:

```toml
id = "attic"
name = "attic"
version = "0.1.0"
min_herdr_version = "0.8.0"
description = "Archive idle agent panes and bring them back"
platforms = ["macos", "linux"]

[[panes]]
id = "control"
title = "attic"
placement = "tab"
command = ["uv", "run", "--project", ".", "attic", "ui"]

[[actions]]
id = "pin"
title = "Pin pane (never reap)"
contexts = ["pane"]
command = ["uv", "run", "--project", ".", "attic", "pin"]
```

**Two things to confirm against herdr before implementing, not to assume:**

1. Whether `--project .` resolves — i.e. whether herdr sets the entrypoint's
   working directory to the linked plugin directory. If it does not, `install.sh`
   writes an absolute path into the manifest at install time.
2. How an `[[actions]]` entrypoint learns *which* pane it was invoked on. The
   socket schema names `PluginActionContext` and `PluginInvocationContext`, but
   whether the pane ID arrives as argv or environment is unverified. Guessing a
   response shape is exactly the mistake that would have broken every restore in
   the archiver; this one gets probed against a live plugin first.

herdr injects `HERDR_SOCKET_PATH`, `HERDR_BIN_PATH`, `HERDR_PLUGIN_ID`,
`HERDR_PLUGIN_CONFIG_DIR`, and `HERDR_PLUGIN_STATE_DIR` into the entrypoint.
`HERDR_SOCKET_PATH` is the important one: it makes the TUI target whichever
session launched it, automatically. During staging this had to be arranged by
hand, and getting it wrong silently pointed a test at the live server.

Installed for development with `herdr plugin link`.

## Backend changes

These are the only changes that affect reap behaviour.

### Snooze and pin

`PaneState` gains two fields, in the same `state.json`, keyed the same way:

```python
@dataclass
class PaneState:
    first_idle_at: str | None
    last_revision: int
    snooze_until: str | None = None   # ISO8601 UTC — expires on its own
    pinned: bool = False              # indefinite — explicit unpin required
```

`update_state` carries both through when rebuilding an entry, and clears an
expired `snooze_until` so state does not accumulate stale timestamps.

The checks live **inside `decide()`**, not beside it. Unlike the resumability
gate — which needs filesystem I/O and therefore sits outside the pure policy —
snooze and pin are already in the state dict `decide()` receives. They become two
skip predicates in `_verdict`, ordered before the idle checks, producing reasons
that read plainly in `reap --dry-run`:

```
skip  w4:p2  Debug batch…   (pinned)
skip  w3:pB  Update wh-win…   (snoozed until 2026-08-14T02:00:00Z)
```

Three decisions, stated explicitly because each has an obvious alternative:

- **Snooze is an absolute deadline, not a clock reset.** `snooze 24h` means *not
  reapable before then*, regardless of accrued idle time. "Extend the clock"
  would be ambiguous about what happens at expiry.
- **Protection does not stop the idle clock.** A pinned pane keeps accruing idle
  time, so unpinning makes it immediately eligible if past threshold. This is the
  same rule already settled for `PAUSE`: guards gate *execution*, never
  *observation*, and `reap --dry-run` shows `pinned` throughout so nothing is
  hidden.
- **Manual archive does not bypass the resumability gate.** `:archive` skips the
  *threshold*, never the check that the session can come back. Forcing an
  unrecoverable close by hand would defeat the guarantee the tool exists to make.
  Invoked on a pane whose transcript is not yet written, `:archive` refuses with
  the gate's own reason rather than proceeding or failing silently.

### CLI verbs

```
attic pin <id>          attic unpin <id>
attic snooze <id> 24h   attic unsnooze <id>
```

Durations are `30m` / `4h` / `2d`; anything else is rejected loudly rather than
guessed. No `attic protected` listing verb — `reap --dry-run` already answers
"what is exempt and why", and a second way to ask one question is one more thing
to keep honest.

**`attic pin w4:p2` resolves the pane ID to its `terminal_id` via herdr at
command time and stores the pin under the terminal ID.** Pane IDs are positional
and get recycled; pinning by pane ID would protect the *slot*, so a brand-new
session opening into `w4:p2` would silently inherit the pin. This is the
recycled-identifier hazard from the archiver's idle clock arriving through a new
door.

### Verdicts recorded in the inventory

`append_inventory` already writes one entry per pane per tick. Each entry gains
`verdict` (`archive` / `skip`) and `reason`. The Activity view then reads data
that already exists rather than parsing `attic.log`, and "why didn't it reap that
pane last Tuesday?" becomes answerable. Entries written before this change lack
the fields and are tolerated.

## The TUI

Three tabs. Actions are not a view — they are commands acting on the current row.

| View | Reads | Answers |
|---|---|---|
| **Fleet** | `herdr pane list` → `decide()` | what is about to be reaped, with live idle clocks |
| **Activity** | `inventory/*.jsonl`, `archive/index.jsonl` | what attic did, and why |
| **Attic** | `load_manifests()` + scrollback | where a session went, and how to get it back |

A pane blocked by the resumability gate is visibly marked in Fleet, because "why
will this never be reaped?" is otherwise a question requiring a dig.

### Motions

| Keys | Effect |
|---|---|
| `j` `k` | row down / up |
| `gg` `G` | first / last row |
| `ctrl+d` `ctrl+u` | half page down / up |
| `ctrl+f` `ctrl+b` | full page down / up |
| `h` `l` | move between panels within a view (table ↔ preview) |
| `/` `?` | search forward / backward |
| `n` `N` | next / previous match |
| `gt` `gT` | next / previous tab |
| `1gt` `2gt` `3gt` | jump to Fleet / Activity / Attic |
| `zz` | centre current row |

### Mutations go through `:`

```
:pin        :unpin        :snooze 4h      :unsnooze
:archive    :restore      :q          :help
```

Two reasons, the second being the operative one:

- **It is vim's own safety model.** You do not remove a file in vim with a single
  keystroke; you type a command. That typing friction *is* the confirmation,
  which is why `:archive` needs no separate y/n prompt.
- **It leaves the entire single-key namespace free for motions.** No collisions to
  negotiate, and no "`p` means paste except here, where it pins."

The single keys that remain are read-only:

| Key | Effect |
|---|---|
| `enter` | open — scrollback preview (Fleet) or manifest + scrollback (Attic) |
| `esc` | close preview / clear search |
| `R` | force refresh |
| `q` | quit (`:q` also works) |

Help is `:help` rather than `?`, because `?` is search-backward — the collision
my first draft had, and the reason bindings get reviewed rather than assumed.

`q` quits rather than recording a macro — a deliberate break from vim, since
nobody records macros in a dashboard and `q`-to-quit is the stronger convention
in a TUI.

Command line supports history (`↑`/`↓`) and tab-completion of command names.
`:snooze` with no argument errors rather than assuming a default duration.
Commands act on the **current row**; a command that does not apply to the current
tab says so rather than doing something surprising.

Counts (`3j`) may be included if cheap. Ranges (`:1,5snooze`) are out of scope —
substantial machinery for a list rarely longer than a dozen rows.

### Refresh

Poll every 2 seconds: re-read `state.json`, call `herdr pane list`, run
`update_state()` then `decide()` against fresh data — the same sequence
`run_tick` uses, minus any writes. The TUI never persists state; only the timer
and the explicit CLI verbs do. herdr's socket offers
`events.subscribe` with `pane.agent_status_changed` for push, which is the
obvious upgrade — but it replays a state snapshot before streaming and needs
careful buffering, so v1 polls and earns that complexity only if 2s feels laggy.

## Testing

- **Row construction is a pure function** — `(panes, state, now, config) → rows` —
  tested in the existing pytest suite with no UI involved.
- **Duration parsing and the snooze/pin predicates** are pure and tested directly,
  including that an expired snooze stops protecting and that `blocked` remains
  unreapable regardless of pin state.
- **`terminal_id` resolution** is tested against the recycled-pane-ID case: pinning
  `w4:p2` and then having a new terminal occupy that pane ID must not inherit the
  pin.
- **Textual's `App.run_test()` pilot** covers keybindings, including that no
  single keystroke can archive a pane and that `:archive` respects the
  resumability gate.
- **The daemon path stays clean:** a test asserts nothing reachable from
  `attic tick` imports `textual`.

## Success criteria

- Every question in the Problem section has a one-screen answer.
- `reap --dry-run` and the Fleet view never disagree, because they call the same
  function.
- A pinned or snoozed pane is never archived, by the timer or by hand.
- `:archive` cannot close a session that `attic restore` could not bring back.
- `attic tick` continues to run under launchd with stdlib only.
