"""The `:` command line — every mutation the TUI can perform.

Mutations are commands rather than keystrokes for two reasons. It is vim's own
safety model: you do not delete a file with a single key, you type a command,
and that typing friction IS the confirmation. And it leaves the entire
single-key namespace free for motions, with no collisions to negotiate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..archive import Archiver
from ..catalog import resolve_id
from ..duration import parse_duration
from ..exempt import resolve_terminal_id, set_pinned, set_snooze
from ..policy import Archive, iso
from ..resumable import resume_blocker
from ..restore import restore
from ..store import AtticHome

FLEET_COMMANDS = {"pin", "unpin", "snooze", "unsnooze", "archive"}
ATTIC_COMMANDS = {"restore"}
KNOWN = FLEET_COMMANDS | ATTIC_COMMANDS | {"q", "quit", "help"}


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class CommandContext:
    home: AtticHome
    client: object
    tab: str
    row_key: str | None
    now: datetime
    projects_root: Path | None = None


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.lstrip(":").split()
    if not parts:
        raise ValueError("empty command")
    verb, args = parts[0], parts[1:]
    if verb not in KNOWN:
        raise ValueError(f"unknown command {verb!r}")
    return verb, args


def _pane_for(ctx: CommandContext):
    for pane in ctx.client.pane_list():
        if pane.pane_id == ctx.row_key:
            return pane
    return None


def run_command(verb: str, args: list[str], ctx: CommandContext) -> CommandResult:
    if verb in ("q", "quit"):
        return CommandResult(True, "")
    if verb == "help":
        return CommandResult(True, "commands: " + " ".join(sorted(KNOWN)))

    if verb in FLEET_COMMANDS and ctx.tab != "fleet":
        return CommandResult(False, f":{verb} applies to the Fleet tab")
    if verb in ATTIC_COMMANDS and ctx.tab != "attic":
        return CommandResult(False, f":{verb} applies to the Attic tab")
    if ctx.row_key is None:
        return CommandResult(False, "no row selected")

    if verb == "restore":
        try:
            manifest = resolve_id(ctx.home, ctx.row_key)
            pane_id = restore(ctx.home, ctx.client, manifest, ctx.now)
        except (LookupError, FileNotFoundError, OSError) as exc:
            return CommandResult(False, str(exc))
        return CommandResult(True, f"restored into {pane_id}")

    pane = _pane_for(ctx)
    if pane is None:
        return CommandResult(False, f"no live pane {ctx.row_key}")
    terminal_id = resolve_terminal_id(ctx.client.pane_list(), pane.pane_id)

    if verb in ("pin", "unpin"):
        set_pinned(ctx.home, terminal_id, verb == "pin")
        return CommandResult(True, f"{verb}ned {pane.pane_id}")

    if verb == "unsnooze":
        set_snooze(ctx.home, terminal_id, None)
        return CommandResult(True, f"snooze cleared for {pane.pane_id}")

    if verb == "snooze":
        if not args:
            return CommandResult(False, "snooze needs a duration, e.g. :snooze 4h")
        try:
            until = ctx.now + parse_duration(args[0])
        except ValueError as exc:
            return CommandResult(False, str(exc))
        previous = set_snooze(ctx.home, terminal_id, until)
        message = f"snoozed until {iso(until)}"
        if previous:
            message += f" (was {previous})"
        if ctx.home.load_state()[terminal_id].pinned:
            message += " — note: pinned, so this applies only after :unpin"
        return CommandResult(True, message)

    # archive: skips the threshold, never the recoverability check.
    blocker = resume_blocker(pane, ctx.projects_root)
    if blocker is not None:
        return CommandResult(False, f"refusing: {blocker}")
    label = ctx.client.workspace_labels().get(pane.workspace_id, pane.workspace_id)
    path = Archiver(ctx.home, ctx.client).archive(Archive(pane, ctx.now), label, ctx.now)
    if path is None:
        return CommandResult(False, "archive failed; pane left alive")
    ctx.client.pane_close(pane.pane_id)
    return CommandResult(True, f"archived {pane.pane_id} as {path.name}")
