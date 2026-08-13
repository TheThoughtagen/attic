"""Command-line entry point and the tick orchestrator."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .archive import Archiver
from .catalog import format_list, load_manifests, resolve_id
from .herdr import HerdrClient, HerdrError
from .inventory import append_inventory, prune_archives, prune_inventory
from .policy import Action, Archive, decide, update_state
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


def run_tick(home: AtticHome, client, now: datetime, dry_run: bool = False) -> TickResult:
    """Snapshot always; reap only when every guard passes. Never raises."""
    home.ensure()
    config = home.load_config()

    try:
        panes = client.pane_list()
        labels = client.workspace_labels()
    except HerdrError as exc:
        log.error("herdr unavailable, skipping tick: %s", exc)
        return TickResult(reason=f"herdr unavailable: {exc}")

    append_inventory(home, panes, labels, now)
    for path in prune_inventory(home, now, config.inventory_retention_days):
        log.info("pruned inventory %s", path.name)
    for path in prune_archives(home, now, config.archive_retention_days):
        log.info("pruned archive %s", path.name)

    state = update_state(panes, home.load_state(), now)
    home.save_state(state)

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

    actions = decide(panes, state, now, config)

    if dry_run:
        return TickResult(actions=actions, reason=blocked or "dry-run")
    if blocked:
        log.info("reaping disabled: %s", blocked)
        return TickResult(actions=actions, reason=blocked)

    archiver = Archiver(home, client)
    archived: list[str] = []
    for action in actions:
        if not isinstance(action, Archive):
            continue
        pane = action.pane
        label = labels.get(pane.workspace_id, pane.workspace_id)
        path = archiver.archive(action, label, now)
        if path is None:
            log.warning("archive failed for %s, leaving it alive", pane.pane_id)
            continue
        close_failed = False
        try:
            client.pane_close(pane.pane_id)
        except HerdrError as exc:
            close_failed = True
            log.error("archived %s but close failed: %s", pane.pane_id, exc)
        entry = {
            "id": path.name,
            "pane_id": pane.pane_id,
            "title": pane.title,
            "cwd": pane.cwd,
            "session_uuid": pane.session_uuid,
            "archived_at": now.isoformat().replace("+00:00", "Z"),
            "close_failed": close_failed,
        }
        try:
            archiver.append_index(entry)
        except OSError as exc:
            # The archive directory and its manifest are already durable, and
            # `attic list` reads manifests from disk rather than this index, so the
            # session stays discoverable and restorable. Only the append-only log
            # loses this entry and its close_failed marker.
            log.error("archived %s but index append failed: %s", pane.pane_id, exc)
        archived.append(path.name)
        log.info("archived %s as %s", pane.pane_id, path.name)

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

    args = parser.parse_args(argv)

    try:
        home = AtticHome.default()
        _setup_logging(home)
    except Exception:
        # Logging is not up yet, so stderr is the only channel available. Still
        # return 0: a crashing timer stops protecting the user, and under launchd
        # the crash itself produces no visible symptom.
        traceback.print_exc(file=sys.stderr)
        return 0

    now = datetime.now(timezone.utc)

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
                return 0
            print(json.dumps(manifest, indent=2))
            print("\n--- scrollback ---\n")
            scrollback = home.archive_dir / manifest["id"] / "scrollback.txt"
            try:
                print(scrollback.read_text(encoding="utf-8"))
            except OSError as exc:
                # A partial archive still has a usable manifest and resume command.
                print(f"(scrollback unavailable: {exc})", file=sys.stderr)
    except Exception:                       # never crash the LaunchAgent loop
        log.exception("unhandled error in %s", args.command)
    return 0
