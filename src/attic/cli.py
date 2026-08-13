"""Command-line entry point and the tick orchestrator."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .archive import Archiver
from .herdr import HerdrClient, HerdrError
from .inventory import append_inventory, prune_archives, prune_inventory
from .policy import Action, Archive, Skip, decide, update_state
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

    if home.is_paused():
        log.info("PAUSE present, reaping disabled")
        return TickResult(reason="paused")

    try:
        protocol = client.protocol()
    except HerdrError as exc:
        return TickResult(reason=f"herdr protocol unreadable: {exc}")
    if protocol != config.herdr_protocol:
        log.warning(
            "herdr protocol %s != pinned %s, reaping disabled", protocol, config.herdr_protocol
        )
        return TickResult(reason=f"protocol mismatch: {protocol} != {config.herdr_protocol}")

    actions = decide(panes, state, now, config)
    if dry_run:
        return TickResult(actions=actions, reason="dry-run")

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
        archiver.append_index(entry)
        archived.append(path.name)
        log.info("archived %s as %s", pane.pane_id, path.name)

    return TickResult(actions=actions, archived=archived, reaped=True, reason="ok")


def _print_verdicts(result: TickResult) -> None:
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

    args = parser.parse_args(argv)
    home = AtticHome.default()
    _setup_logging(home)
    now = datetime.now(timezone.utc)

    try:
        if args.command == "tick":
            result = run_tick(home, HerdrClient(), now)
            print(f"archived {len(result.archived)} pane(s); {result.reason}")
        elif args.command == "reap":
            result = run_tick(home, HerdrClient(), now, dry_run=args.dry_run)
            _print_verdicts(result)
    except Exception:                       # never crash the LaunchAgent loop
        log.exception("unhandled error in %s", args.command)
    return 0
