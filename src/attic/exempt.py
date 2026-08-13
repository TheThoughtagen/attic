"""Applying pin and snooze exemptions.

Everything is keyed by `terminal_id`, never `pane_id`. Pane IDs are positional
and get recycled when panes close and reopen, so a pin stored under "w4:p2"
would protect the *slot* — and a brand-new session opening into that pane would
silently inherit the protection. The identifier is resolved at command time.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime

from .models import Pane
from .policy import iso
from .store import AtticHome, PaneState

# setattr would accept a typo'd field name, create a phantom attribute, and let
# save_state silently drop it — a mutation that reports success and never
# persists. For a feature whose whole job is "this pane is protected", a silent
# no-op is worse than a crash.
_PANE_STATE_FIELDS = {f.name for f in fields(PaneState)}


def resolve_terminal_id(panes: list[Pane], identifier: str) -> str:
    """Accept either a pane id (w4:p2) or a terminal id (term_abc)."""
    for pane in panes:
        if pane.terminal_id == identifier:
            return identifier
    for pane in panes:
        if pane.pane_id == identifier:
            return pane.terminal_id
    raise LookupError(f"no live pane matching {identifier!r}")


def _mutate(home: AtticHome, terminal_id: str, **changes) -> PaneState:
    """Read-modify-write a single entry, leaving the idle clock untouched."""
    unknown = set(changes) - _PANE_STATE_FIELDS
    if unknown:
        raise ValueError(f"not a PaneState field: {', '.join(sorted(unknown))}")
    state = home.load_state()
    entry = state.get(terminal_id) or PaneState(None, 0)
    for field, value in changes.items():
        setattr(entry, field, value)
    state[terminal_id] = entry
    home.save_state(state)
    return entry


def set_pinned(home: AtticHome, terminal_id: str, pinned: bool) -> None:
    _mutate(home, terminal_id, pinned=pinned)


def set_snooze(home: AtticHome, terminal_id: str, until: datetime | None) -> str | None:
    """Set or clear the deadline. Returns the previous one, if any.

    Re-snoozing replaces rather than stacks — stacking would let repeated
    snoozes compound invisibly into days of protection, which is the silent
    accumulation this tool exists to prevent. Because replacing can therefore
    shorten a snooze, the previous value is returned so the caller can report it.
    """
    previous = home.load_state().get(terminal_id)
    previous_until = previous.snooze_until if previous else None
    _mutate(home, terminal_id, snooze_until=iso(until) if until else None)
    return previous_until
