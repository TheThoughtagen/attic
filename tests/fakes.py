"""Programmable HerdrClient double. Failures are opt-in per pane id."""

from __future__ import annotations

from attic.herdr import HerdrError
from attic.models import Pane


class FakeHerdrClient:
    def __init__(self, panes: list[Pane] | None = None, protocol: int = 19,
                 labels: dict[str, str] | None = None) -> None:
        self.panes = panes or []
        self._protocol = protocol
        self.labels = labels or {}
        self.scrollback = "line one\nline two\n"
        # Programmable failures, keyed by pane id:
        self.fail_read: set[str] = set()
        self.fail_close: set[str] = set()
        self.empty_read: set[str] = set()
        # Observations:
        self.closed: list[str] = []
        self.reads: list[tuple[str, int]] = []
        self.ran: list[tuple[str, list[str]]] = []
        self.created_tabs: list[tuple[str, str]] = []
        self.next_pane_id = "w9:p9"

    def protocol(self) -> int:
        return self._protocol

    def pane_list(self) -> list[Pane]:
        return list(self.panes)

    def snapshot(self) -> dict:
        return {"result": {"panes": [p.pane_id for p in self.panes]}}

    def workspace_labels(self) -> dict[str, str]:
        return dict(self.labels)

    def pane_read(self, pane_id: str, lines: int) -> str:
        self.reads.append((pane_id, lines))
        if pane_id in self.fail_read:
            raise HerdrError(f"simulated read failure for {pane_id}")
        if pane_id in self.empty_read:
            return ""
        return self.scrollback

    def pane_close(self, pane_id: str) -> None:
        if pane_id in self.fail_close:
            raise HerdrError(f"simulated close failure for {pane_id}")
        self.closed.append(pane_id)

    def tab_create(self, cwd: str, label: str) -> str:
        self.created_tabs.append((cwd, label))
        return self.next_pane_id

    def pane_run(self, pane_id: str, command: list[str]) -> None:
        self.ran.append((pane_id, command))
