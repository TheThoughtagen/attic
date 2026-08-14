"""Pure row construction for the TUI.

No Textual import: these functions carry the view's real logic and are tested
without a UI. Textual only renders what they return.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from .catalog import load_manifests
from .evaluate import Evaluation
from .policy import Archive
from .store import AtticHome


@dataclass(frozen=True)
class FleetRow:
    pane_id: str
    workspace: str
    status: str
    idle_for: str
    verdict: str
    reason: str
    terminal_id: str


@dataclass(frozen=True)
class ActivityRow:
    at: str
    pane_id: str
    title: str
    verdict: str
    reason: str


@dataclass(frozen=True)
class AtticRow:
    archive_id: str
    archived_at: str
    workspace: str
    title: str


def humanize(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _idle_seconds(evaluation: Evaluation, terminal_id: str, now: datetime) -> float | None:
    entry = evaluation.state.get(terminal_id)
    if entry is None or entry.first_idle_at is None:
        return None
    since = datetime.fromisoformat(entry.first_idle_at.replace("Z", "+00:00"))
    return (now - since).total_seconds()


def fleet_rows(evaluation: Evaluation, now: datetime) -> list[FleetRow]:
    """Sorted by how close each pane is to being reaped, so the thing about to
    happen sits at the top where it will be seen."""
    by_pane = {a.pane.pane_id: a for a in evaluation.actions}
    rows: list[tuple[float, FleetRow]] = []
    for pane in evaluation.panes:
        action = by_pane.get(pane.pane_id)
        idle = _idle_seconds(evaluation, pane.terminal_id, now)
        archiving = isinstance(action, Archive)
        rows.append((
            # Archives first, then longest-idle. Negative so bigger sorts earlier.
            (-1e12 if archiving else 0) - (idle or 0),
            FleetRow(
                pane_id=pane.pane_id,
                workspace=evaluation.labels.get(pane.workspace_id, pane.workspace_id),
                status=pane.agent_status,
                idle_for=humanize(idle),
                verdict="archive" if archiving else "skip",
                reason="" if archiving else getattr(action, "reason", ""),
                terminal_id=pane.terminal_id,
            ),
        ))
    return [row for _, row in sorted(rows, key=lambda pair: pair[0])]


def activity_rows(home: AtticHome, limit: int = 200) -> list[ActivityRow]:
    """Newest first. A corrupt line is skipped, never fatal — this view is what
    someone reads when they are already trying to work out what went wrong."""
    rows: list[ActivityRow] = []
    if not home.inventory_dir.exists():
        return rows
    for path in sorted(home.inventory_dir.glob("*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                panes = entry["panes"]
                # Inside the guard: a non-list `panes` parses fine and then raises
                # TypeError at the loop below, destroying the whole view over one
                # bad line. This is the view someone opens when something has
                # ALREADY gone wrong.
                if not isinstance(panes, list):
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            for pane in panes:
                if not isinstance(pane, dict):
                    continue
                rows.append(ActivityRow(
                    at=entry.get("at", "—"),
                    pane_id=pane.get("pane_id", "—"),
                    title=pane.get("title", ""),
                    verdict=pane.get("verdict") or "—",
                    reason=pane.get("reason") or "",
                ))
                if len(rows) >= limit:
                    return rows
    return rows


def attic_rows(home: AtticHome) -> list[AtticRow]:
    return [
        AtticRow(
            archive_id=m["id"],
            archived_at=m.get("archived_at", "—"),
            workspace=m.get("workspace", ""),
            title=m.get("title", ""),
        )
        for m in load_manifests(home)
    ]
