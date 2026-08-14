"""The attic control surface.

Imported only by `attic ui`. Nothing on the tick path may import this module or
textual — `tests/test_daemon_purity.py` enforces that, because a broken TUI
dependency must never be able to stop the reaper.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, TabbedContent, TabPane

from ..evaluate import evaluate
from ..rows import activity_rows, attic_rows, fleet_rows
from ..store import AtticHome
from .motions import MotionState

REFRESH_SECONDS = 2.0


class AtticApp(App):
    """Three views over attic's own policy functions, called in-process."""

    CSS = "DataTable { height: 1fr; }"
    TITLE = "attic"

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("ctrl+d", "half_down", "½ down", show=False),
        Binding("ctrl+u", "half_up", "½ up", show=False),
        Binding("ctrl+f", "page_down", "page down", show=False),
        Binding("ctrl+b", "page_up", "page up", show=False),
        Binding("R", "force_refresh", "refresh"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, home: AtticHome, client, projects_root: Path | None = None) -> None:
        super().__init__()
        self.home = home
        self.client = client
        self.projects_root = projects_root
        self.last_error: str | None = None
        self.motions = MotionState()

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="fleet"):
            with TabPane("Fleet", id="fleet"):
                yield DataTable(id="fleet-table", cursor_type="row")
            with TabPane("Activity", id="activity"):
                yield DataTable(id="activity-table", cursor_type="row")
            with TabPane("Attic", id="attic"):
                yield DataTable(id="attic-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#fleet-table", DataTable).add_columns(
            "pane", "workspace", "status", "idle", "verdict", "reason")
        self.query_one("#activity-table", DataTable).add_columns(
            "at", "pane", "title", "verdict", "reason")
        self.query_one("#attic-table", DataTable).add_columns(
            "id", "archived", "workspace", "title")
        self.refresh_data()
        self.set_interval(REFRESH_SECONDS, self.refresh_data)

    def refresh_data(self) -> None:
        """Re-read everything. Never writes state — watching the dashboard must
        not advance idle clocks or change what the timer does."""
        try:
            now = datetime.now(timezone.utc)
            ev = evaluate(self.home, self.client, now, self.projects_root)
            self.last_error = None
        except Exception as exc:            # herdr down, malformed payload, etc.
            self.last_error = str(exc)
            return

        fleet = self.query_one("#fleet-table", DataTable)
        fleet.clear()
        for row in fleet_rows(ev, now):
            fleet.add_row(row.pane_id, row.workspace, row.status,
                          row.idle_for, row.verdict, row.reason, key=row.pane_id)

        activity = self.query_one("#activity-table", DataTable)
        activity.clear()
        for row in activity_rows(self.home):
            activity.add_row(row.at, row.pane_id, row.title, row.verdict, row.reason)

        attic = self.query_one("#attic-table", DataTable)
        attic.clear()
        for row in attic_rows(self.home):
            attic.add_row(row.archive_id, row.archived_at, row.workspace, row.title,
                          key=row.archive_id)

    def _table(self) -> DataTable:
        pane = self.query_one(TabbedContent).active
        return self.query_one(f"#{pane}-table", DataTable)

    def action_cursor_down(self) -> None:
        self._table().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._table().action_cursor_up()

    def action_half_down(self) -> None:
        for _ in range(10):
            self._table().action_cursor_down()

    def action_half_up(self) -> None:
        for _ in range(10):
            self._table().action_cursor_up()

    def action_page_down(self) -> None:
        self._table().action_page_down()

    def action_page_up(self) -> None:
        self._table().action_page_up()

    def action_force_refresh(self) -> None:
        self.refresh_data()

    def on_key(self, event) -> None:
        """Two-key sequences Textual's binding table cannot express."""
        action = self.motions.feed(event.key)
        if action is None:
            return
        table = self._table()
        tabs = self.query_one(TabbedContent)
        if action == "top":
            table.move_cursor(row=0)
        elif action == "bottom":
            table.move_cursor(row=max(table.row_count - 1, 0))
        elif action in ("next_tab", "prev_tab"):
            order = ["fleet", "activity", "attic"]
            index = order.index(tabs.active)
            step = 1 if action == "next_tab" else -1
            tabs.active = order[(index + step) % len(order)]
        elif action.startswith("tab_"):
            order = ["fleet", "activity", "attic"]
            wanted = int(action.removeprefix("tab_")) - 1
            if 0 <= wanted < len(order):
                tabs.active = order[wanted]
        event.stop()
