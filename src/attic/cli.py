"""Command-line entry point and the tick orchestrator."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .catalog import format_list, load_manifests, resolve_id
from .duration import parse_duration
from .evaluate import archive_and_close, evaluate
from .exempt import resolve_terminal_id, set_pinned, set_snooze
from .herdr import HerdrClient, HerdrError
from .inventory import append_inventory, prune_archives, prune_inventory
from .policy import Action, Archive, iso
from .restore import restore as restore_archive
from .store import AtticHome

log = logging.getLogger("attic")


@dataclass(frozen=True)
class TickResult:
    actions: list[Action] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    reaped: bool = False
    reason: str = ""


def _setup_logging(home: AtticHome) -> None:
    home.ensure()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(home.log_path), logging.StreamHandler(sys.stderr)],
    )


def run_tick(
    home: AtticHome,
    client,
    now: datetime,
    dry_run: bool = False,
    projects_root: Path | None = None,
) -> TickResult:
    """Snapshot always; reap only when every guard passes. Never raises."""
    home.ensure()

    try:
        result = evaluate(home, client, now, projects_root)
    except HerdrError as exc:
        log.error("herdr unavailable, skipping tick: %s", exc)
        return TickResult(reason=f"herdr unavailable: {exc}")
    panes, labels, actions = result.panes, result.labels, result.actions
    config = result.config  # the snapshot the decisions were made under

    for path in prune_inventory(home, now, config.inventory_retention_days):
        log.info("pruned inventory %s", path.name)
    for path in prune_archives(home, now, config.archive_retention_days):
        log.info("pruned archive %s", path.name)

    home.save_state(result.state)

    # Determine whether reaping is permitted, but do NOT return yet: verdicts are
    # computed either way. `attic` installs PAUSED and the soak procedure is "read
    # `attic reap --dry-run` for days, then grant authority by removing PAUSE" — so
    # short-circuiting before decide() would make that output empty and the whole
    # trust-building step impossible.
    blocked: str | None = None
    if home.is_paused():
        blocked = "paused"
    else:
        try:
            protocol = client.protocol()
        except HerdrError as exc:
            blocked = f"herdr protocol unreadable: {exc}"
        else:
            if protocol != config.herdr_protocol:
                log.warning(
                    "herdr protocol %s != pinned %s, reaping disabled",
                    protocol, config.herdr_protocol,
                )
                blocked = f"protocol mismatch: {protocol} != {config.herdr_protocol}"

    # Inventory is written after decide() so it can record WHY each pane was
    # skipped, but it is still unconditional: snapshotting is pure observation
    # and must not be gated on whether reaping is permitted.
    verdicts = {
        a.pane.pane_id: (("archive", "") if isinstance(a, Archive) else ("skip", a.reason))
        for a in actions
    }
    append_inventory(home, panes, labels, now, verdicts)

    if dry_run:
        return TickResult(actions=actions, reason=blocked or "dry-run")
    if blocked:
        log.info("reaping disabled: %s", blocked)
        return TickResult(actions=actions, reason=blocked)

    archived: list[str] = []
    for action in actions:
        if not isinstance(action, Archive):
            continue
        pane = action.pane
        label = labels.get(pane.workspace_id, pane.workspace_id)
        archive_id, message = archive_and_close(home, client, action, label, now)
        if archive_id is None:
            log.warning("archive failed for %s, leaving it alive", pane.pane_id)
            continue
        archived.append(archive_id)
        log.info(message)

    return TickResult(actions=actions, archived=archived, reaped=True, reason="ok")


def _print_verdicts(result: TickResult) -> None:
    # The soak has the user reading this output for days while attic is PAUSED.
    # Without this banner a paused run looks identical to a run with nothing to do.
    if result.reason and result.reason != "dry-run":
        print(f"reaping disabled: {result.reason} — showing what would happen anyway\n")
    for action in result.actions:
        if isinstance(action, Archive):
            print(f"ARCHIVE  {action.pane.pane_id:<8} {action.pane.title}")
        else:
            print(f"skip     {action.pane.pane_id:<8} {action.pane.title}  ({action.reason})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="attic")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tick", help="snapshot inventory and reap idle agent panes")
    reap = sub.add_parser("reap", help="reap now")
    reap.add_argument("--dry-run", action="store_true", help="print verdicts, change nothing")
    sub.add_parser("list", help="list archived sessions")
    show = sub.add_parser("show", help="print an archive's manifest and scrollback")
    show.add_argument("archive_id")
    restore_p = sub.add_parser("restore", help="reopen an archived session")
    restore_p.add_argument("archive_id")

    for verb, helptext in (("pin", "never reap this pane"),
                           ("unpin", "allow this pane to be reaped again"),
                           ("unsnooze", "clear this pane's snooze")):
        parser_ = sub.add_parser(verb, help=helptext)
        parser_.add_argument("identifier", help="pane id (w4:p2) or terminal id")
    snooze_p = sub.add_parser("snooze", help="protect this pane until a deadline")
    snooze_p.add_argument("identifier", help="pane id (w4:p2) or terminal id")
    snooze_p.add_argument("duration", help="30m, 4h, 2d")
    sub.add_parser("ui", help="open the control surface")

    args = parser.parse_args(argv)

    try:
        home = AtticHome.default()
        _setup_logging(home)
    except Exception:  # noqa: BLE001 — logging is not up; nothing may escape here
        # Logging is not up yet, so stderr is the only channel available. Still
        # return 0: a crashing timer stops protecting the user, and under launchd
        # the crash itself produces no visible symptom.
        traceback.print_exc(file=sys.stderr)
        return 0

    now = datetime.now(UTC)

    try:
        if args.command == "tick":
            result = run_tick(home, HerdrClient(), now)
            summary = f"archived {len(result.archived)} pane(s); {result.reason}"
            log.info(summary)          # launchd stdout may go nowhere; the log file persists
            print(summary)
        elif args.command == "reap":
            result = run_tick(home, HerdrClient(), now, dry_run=args.dry_run)
            _print_verdicts(result)
        elif args.command == "list":
            print(format_list(load_manifests(home)))
        elif args.command == "show":
            try:
                manifest = resolve_id(home, args.archive_id)
            except LookupError as exc:
                # Interactive command: a clear message, not a traceback.
                print(str(exc), file=sys.stderr)
                return 1
            print(json.dumps(manifest, indent=2))
            print("\n--- scrollback ---\n")
            scrollback = home.archive_dir / manifest["id"] / "scrollback.txt"
            try:
                print(scrollback.read_text(encoding="utf-8"))
            except OSError as exc:
                # A partial archive still has a usable manifest and resume command.
                print(f"(scrollback unavailable: {exc})", file=sys.stderr)
        elif args.command == "restore":
            try:
                manifest = resolve_id(home, args.archive_id)
            except LookupError as exc:
                # Interactive command: a clear message, not a traceback.
                print(str(exc), file=sys.stderr)
                return 1
            try:
                pane_id = restore_archive(home, HerdrClient(), manifest, now)
            except FileNotFoundError as exc:
                # Show WHAT is being abandoned. The resume string in particular lets
                # the user recover by hand if the directory merely moved.
                print(str(exc), file=sys.stderr)
                print(json.dumps(manifest, indent=2), file=sys.stderr)
                return 1
            except HerdrError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"restored {manifest['id']} into {pane_id}")
        elif args.command in ("pin", "unpin", "snooze", "unsnooze"):
            try:
                panes = HerdrClient().pane_list()
                terminal_id = resolve_terminal_id(panes, args.identifier)
            except (HerdrError, LookupError) as exc:
                print(str(exc), file=sys.stderr)
                return 1

            if args.command in ("pin", "unpin"):
                set_pinned(home, terminal_id, args.command == "pin")
                print(f"{args.command}ned {args.identifier} ({terminal_id})")
                return 0

            if args.command == "unsnooze":
                set_snooze(home, terminal_id, None)
                print(f"snooze cleared for {args.identifier}")
                return 0

            try:
                delta = parse_duration(args.duration)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            until = now + delta
            previous = set_snooze(home, terminal_id, until)
            message = f"snoozed until {iso(until)}"
            if previous:
                message += f" (was {previous})"
            print(message)
            if home.load_state()[terminal_id].pinned:
                print("note: pane is pinned; snooze applies only after unpin")
            return 0
        elif args.command == "ui":
            try:
                from .tui.app import AtticApp
            except ImportError:
                print("attic ui needs textual: 'uv sync --extra tui' in a checkout, "
                      "or reinstall with './install.sh'", file=sys.stderr)
                return 1
            AtticApp(home, HerdrClient()).run()
            return 0
    except Exception:                       # never crash the LaunchAgent loop
        log.exception("unhandled error in %s", args.command)
        return 0 if args.command in ("tick", "reap") else 1
    return 0
