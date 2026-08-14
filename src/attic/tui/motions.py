"""Multi-key vim sequences.

Textual binds single keys natively, but `gg`, `gt`, `gT` and `2gt` are
sequences it does not model. This state machine is deliberately separate from
the app so it can be tested without a UI.
"""

from __future__ import annotations


class MotionState:
    """Accumulates keys until a sequence resolves to an action, or clears."""

    def __init__(self) -> None:
        self._count = ""
        self._awaiting_g = False

    @property
    def pending(self) -> str:
        """What the user has typed so far, for the status line."""
        return self._count + ("g" if self._awaiting_g else "")

    def _clear(self) -> None:
        self._count = ""
        self._awaiting_g = False

    def feed(self, key: str) -> str | None:
        if self._awaiting_g:
            count, self._count = self._count, ""
            self._awaiting_g = False
            if key == "g":
                return "top"
            if key == "t":
                return f"tab_{count}" if count else "next_tab"
            if key == "T":
                return "prev_tab"
            # Any other key abandons the sequence rather than leaving it armed —
            # otherwise the NEXT keystroke does something unasked for.
            return None
        if key == "g":
            self._awaiting_g = True
            return None
        if key.isdigit():
            self._count += key
            return None
        if key == "G":
            self._clear()
            return "bottom"
        self._clear()
        return None
