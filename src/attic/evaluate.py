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

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import Pane
from .policy import Action, Archive, Skip, decide, update_state
from .resumable import resume_blocker
from .store import AtticHome, PaneState


@dataclass(frozen=True)
class Evaluation:
    panes: list[Pane]
    state: dict[str, PaneState]
    actions: list[Action]
    labels: dict[str, str]


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
    state = update_state(panes, home.load_state(), now)
    actions = gate_on_resumability(decide(panes, state, now, config), projects_root)
    return Evaluation(panes=panes, state=state, actions=actions, labels=labels)
