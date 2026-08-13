"""Pure reap policy. No I/O, no clock, no herdr."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Pane
from .store import Config, PaneState

REAPABLE_STATUS = "idle"


@dataclass(frozen=True)
class Archive:
    pane: Pane
    idle_since: datetime


@dataclass(frozen=True)
class Skip:
    pane: Pane
    reason: str


Action = Archive | Skip


def iso(dt: datetime) -> str:
    """Serialize as UTC ISO-8601 with a Z suffix, normalizing first so the
    project-wide UTC contract is enforced here rather than trusted at every
    call site. Three later modules import this."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def update_state(
    panes: list[Pane], state: dict[str, PaneState], now: datetime
) -> dict[str, PaneState]:
    """Maintain the idle clock. Panes that vanished are dropped."""
    updated: dict[str, PaneState] = {}
    for pane in panes:
        prior = state.get(pane.terminal_id)
        if pane.agent_status != REAPABLE_STATUS:
            updated[pane.terminal_id] = PaneState(None, pane.revision)
            continue
        if prior is None or prior.last_revision != pane.revision or prior.first_idle_at is None:
            updated[pane.terminal_id] = PaneState(iso(now), pane.revision)
        else:
            updated[pane.terminal_id] = PaneState(prior.first_idle_at, pane.revision)
    return updated


def _verdict(pane: Pane, state: dict[str, PaneState], now, config) -> Skip | datetime:
    """Return a Skip, or the datetime the pane went idle if it qualifies."""
    if not pane.is_agent:
        return Skip(pane, "not an agent pane")
    if pane.agent_status != REAPABLE_STATUS:
        return Skip(pane, f"status is {pane.agent_status}")
    if not pane.session_uuid:
        return Skip(pane, "no session uuid")
    if pane.focused:
        return Skip(pane, "focused")
    entry = state.get(pane.terminal_id)
    if entry is None or entry.first_idle_at is None:
        return Skip(pane, "idle clock not started")
    since = _parse(entry.first_idle_at)
    if (now - since).total_seconds() < config.idle_threshold_hours * 3600:
        return Skip(pane, "not idle long enough")
    return since


def decide(
    panes: list[Pane], state: dict[str, PaneState], now: datetime, config: Config
) -> list[Action]:
    """Return one verdict per pane, preserving input order."""
    verdicts: dict[str, Skip | datetime] = {
        p.pane_id: _verdict(p, state, now, config) for p in panes
    }
    eligible = sorted(
        (p for p in panes if isinstance(verdicts[p.pane_id], datetime)),
        key=lambda p: verdicts[p.pane_id],           # oldest idle first
    )
    approved = {p.pane_id for p in eligible[: config.per_tick_cap]}

    actions: list[Action] = []
    for pane in panes:
        v = verdicts[pane.pane_id]
        if isinstance(v, Skip):
            actions.append(v)
        elif pane.pane_id in approved:
            actions.append(Archive(pane, v))
        else:
            actions.append(Skip(pane, "per-tick cap reached"))
    return actions
