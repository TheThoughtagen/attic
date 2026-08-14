"""The attic control surface.

Imported only by `attic ui`. Nothing on the tick path may import this module or
textual — `tests/test_daemon_purity.py` enforces that, because a broken TUI
dependency must never be able to stop the reaper.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Input, TabbedContent, TabPane
from textual.widgets.data_table import RowDoesNotExist

from ..evaluate import evaluate
from ..rows import activity_rows, attic_rows, fleet_rows
from ..store import AtticHome
from .motions import MotionState

REFRESH_SECONDS = 2.0


class AtticApp(App):
    """Three views over attic's own policy functions, called in-process."""

    CSS = "DataTable { height: 1fr; }"
    TITLE = "attic"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("ctrl+d", "half_down", "½ down", show=False),
        Binding("ctrl+u", "half_up", "½ up", show=False),
        Binding("ctrl+f", "page_down", "page down", show=False),
        Binding("ctrl+b", "page_up", "page up", show=False),
        Binding("R", "force_refresh", "refresh"),
        Binding("q", "quit", "quit"),
        Binding(":", "open_command", "command"),
    ]

    def __init__(self, home: AtticHome, client, projects_root: Path | None = None) -> None:
        super().__init__()
        self.home = home
        self.client = client
        self.projects_root = projects_root
        self.last_error: str | None = None
        self.motions = MotionState()
        # Captured at `:`-open time, not read again at Enter — see action_open_command.
        self._command_row_key: str | None = None
        self._command_tab: str | None = None

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="fleet"):
            with TabPane("Fleet", id="fleet"):
                yield DataTable(id="fleet-table", cursor_type="row")
            with TabPane("Activity", id="activity"):
                yield DataTable(id="activity-table", cursor_type="row")
            with TabPane("Attic", id="attic"):
                yield DataTable(id="attic-table", cursor_type="row")
        yield Input(id="command", placeholder=":")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#fleet-table", DataTable).add_columns(
            "pane", "workspace", "status", "idle", "verdict", "reason")
        self.query_one("#activity-table", DataTable).add_columns(
            "at", "pane", "title", "verdict", "reason")
        self.query_one("#attic-table", DataTable).add_columns(
            "id", "archived", "workspace", "title")
        cmd = self.query_one("#command", Input)
        cmd.display = False
        # AUTO_FOCUS resolves during Screen._compose(), BEFORE on_mount runs — so a
        # focusable hidden Input wins the race and silently swallows every keystroke.
        # Making it unfocusable while hidden removes the race rather than out-running it.
        cmd.can_focus = False
        self.set_focus(self.query_one("#fleet-table", DataTable))
        self.refresh_data()
        self.set_interval(REFRESH_SECONDS, self.refresh_data)

    def refresh_data(self) -> None:
        """Re-read everything. Never writes state — watching the dashboard must
        not advance idle clocks or change what the timer does."""
        if self.query_one("#command", Input).display:
            return      # never move the ground under a command being typed
        try:
            now = datetime.now(UTC)
            ev = evaluate(self.home, self.client, now, self.projects_root)
            self.last_error = None
            self.sub_title = ""
        except Exception as exc:  # noqa: BLE001 — herdr down, malformed payload, etc.
            self.last_error = str(exc)
            self.sub_title = f"stale — {exc}"
            self.notify(str(exc), severity="error")
            return

        fleet = self.query_one("#fleet-table", DataTable)
        selected = self._selected_key(fleet)
        fleet.clear()
        for row in fleet_rows(ev, now):
            fleet.add_row(row.pane_id, row.workspace, row.status,
                          row.idle_for, row.verdict, row.reason, key=row.pane_id)
        self._restore_selection(fleet, selected)

        activity = self.query_one("#activity-table", DataTable)
        activity.clear()
        for row in activity_rows(self.home):
            activity.add_row(row.at, row.pane_id, row.title, row.verdict, row.reason)

        attic = self.query_one("#attic-table", DataTable)
        selected = self._selected_key(attic)
        attic.clear()
        for row in attic_rows(self.home):
            attic.add_row(row.archive_id, row.archived_at, row.workspace, row.title,
                          key=row.archive_id)
        self._restore_selection(attic, selected)

    @staticmethod
    def _selected_key(table: DataTable) -> str | None:
        """The row key under the cursor, or None if there is no usable selection."""
        if not table.row_count or table.cursor_row is None:
            return None
        try:
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception:  # noqa: BLE001 — no usable selection is not an error
            return None

    @staticmethod
    def _restore_selection(table: DataTable, key: str | None) -> None:
        """Put the cursor back on the same ROW KEY, not the same row index.

        refresh_data clears and repopulates every 2s. Without this the cursor
        snaps to row 0, so a user who selects a pane and pauses to type
        `:archive` would archive whichever pane happens to sort first — a
        mutation on a different session than the one displayed.

        Restoring by key rather than index also survives re-sorting: fleet_rows
        orders by time-to-reap, so rows genuinely move between refreshes.
        """
        if key is None or not table.row_count:
            return
        try:
            table.move_cursor(row=table.get_row_index(key))
        except RowDoesNotExist:
            pass    # that row is gone (pane closed) — leave the cursor where it landed

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

    def action_open_command(self) -> None:
        # Capture the target NOW. Reading the cursor at Enter means a refresh
        # landing while the user types retargets the command onto another pane —
        # and `:archive` closes a live session they did not select.
        self._command_row_key = self._selected_key(self._table())
        self._command_tab = self.query_one(TabbedContent).active
        box = self.query_one("#command", Input)
        box.can_focus = True
        box.display = True
        box.value = ":"
        box.focus()

    def on_input_submitted(self, event) -> None:
        from ..tui.commands import CommandContext, parse_command, run_command
        box = self.query_one("#command", Input)
        box.display = False
        box.can_focus = False
        text = event.value
        box.value = ""
        self.set_focus(self._table())      # return focus to the table, not to nothing
        try:
            verb, args = parse_command(text)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        if verb in ("q", "quit"):
            self.exit()
            return
        row_key = self._command_row_key
        tab = self._command_tab or self.query_one(TabbedContent).active
        try:
            result = run_command(verb, args, CommandContext(
                home=self.home, client=self.client,
                tab=tab,
                row_key=row_key, now=datetime.now(UTC),
                projects_root=self.projects_root))
        except Exception as exc:  # noqa: BLE001 — herdr dying mid-command must not kill the app
            self.notify(f"command failed: {exc}", severity="error")
            return
        self.notify(result.message, severity="information" if result.ok else "error")
        self.refresh_data()
