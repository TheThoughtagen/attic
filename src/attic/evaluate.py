"""The one evaluation pipeline.

`run_tick` and the TUI's Fleet view must agree exactly about what will be
archived. Two call sites running "the same" sequence is the drift this project
has been bitten by three times — most sharply when a guessed herdr response
shape passed every test because the fake encoded the same wrong assumption.
One function, two callers, no room to diverge.

`evaluate()` deliberately does NOT persist state. The TUI polls every two
seconds; if watching the dashboard advanced idle clocks, merely looking at the
tool would change what it does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .archive import Archiver
from .herdr import HerdrError
from .models import Pane
from .policy import Action, Archive, Skip, decide, iso, update_state
from .resumable import resume_blocker
from .store import AtticHome, Config, PaneState

log = logging.getLogger("attic")


@dataclass(frozen=True)
class Evaluation:
    panes: list[Pane]
    state: dict[str, PaneState]
    actions: list[Action]
    labels: dict[str, str]
    config: Config
    """The snapshot the decisions were made under.

    Callers must use this rather than re-reading the config, or a tick can
    decide under one policy and execute under another — archiving a pane the
    user had just pinned or exempted mid-tick.
    """


def gate_on_resumability(
    actions: list[Action], projects_root: Path | None = None
) -> list[Action]:
    """Downgrade Archive verdicts whose session cannot be proven recoverable.

    Applied to the verdicts rather than inside the archive loop so that
    `attic reap --dry-run` and the Fleet view both show what will actually
    happen. A preview promising an archive the tick would refuse is worse than
    no preview at all.
    """
    gated: list[Action] = []
    for action in actions:
        if isinstance(action, Archive):
            blocker = resume_blocker(action.pane, projects_root)
            if blocker is not None:
                gated.append(Skip(action.pane, blocker))
                continue
        gated.append(action)
    return gated


def evaluate(
    home: AtticHome,
    client,
    now: datetime,
    projects_root: Path | None = None,
) -> Evaluation:
    """Read herdr, advance the idle clock in memory, decide, and gate.

    Never writes. Callers that must persist (run_tick) call save_state themselves.
    """
    panes = client.pane_list()
    labels = client.workspace_labels()
    config = home.load_config()
    state = update_state(panes, home.load_state(), now, config)
    actions = gate_on_resumability(decide(panes, state, now, config), projects_root)
    return Evaluation(panes=panes, state=state, actions=actions, labels=labels,
                      config=config)


def archive_and_close(home: AtticHome, client, action: Archive, label: str,
                       now: datetime) -> tuple[str | None, str]:
    """Archive a pane, close it, and record the result. Returns (archive_id, message).

    Shared by run_tick and the TUI's :archive so the two cannot diverge — the
    execution half of the same guarantee evaluate() provides for decisions.
    A None archive_id means nothing was closed.
    """
    archiver = Archiver(home, client)
    path = archiver.archive(action, label, now)
    if path is None:
        return None, "archive failed; pane left alive"
    pane = action.pane
    close_failed = False
    try:
        client.pane_close(pane.pane_id)
    except HerdrError as exc:
        close_failed = True
        log.error("archived %s but close failed: %s", pane.pane_id, exc)
    try:
        archiver.append_index({
            "id": path.name, "pane_id": pane.pane_id,
            "title": pane.title, "cwd": pane.cwd,
            "session_uuid": pane.session_uuid,
            "archived_at": iso(now), "close_failed": close_failed,
        })
    except OSError as exc:
        # The archive directory and its manifest are already durable, and
        # `attic list` reads manifests from disk rather than this index, so the
        # session stays discoverable and restorable. Only the append-only log
        # loses this entry and its close_failed marker.
        log.error("archived %s but index append failed: %s", pane.pane_id, exc)
    return path.name, f"archived {pane.pane_id} as {path.name}"
