import pytest

from attic.store import AtticHome
from attic.tui.app import AtticApp
from fakes import FakeHerdrClient
from test_policy import mkpane

pytestmark = pytest.mark.asyncio


def app_for(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    return AtticApp(home, FakeHerdrClient(panes=[mkpane("w4:p2"), mkpane("w4:p3")]),
                    projects_root=tmp_path / "projects")


def app_for_many(tmp_path):
    """Three panes, so selection-preservation tests have somewhere to move to
    and rows genuinely to reorder."""
    home = AtticHome(tmp_path)
    home.ensure()
    panes = [mkpane("w4:p1"), mkpane("w4:p2"), mkpane("w4:p3")]
    return AtticApp(home, FakeHerdrClient(panes=panes), projects_root=tmp_path / "projects")


async def test_fleet_table_populates(tmp_path):
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#fleet-table")
        assert table.row_count == 2


async def test_j_and_k_move_the_cursor(tmp_path):
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#fleet-table")
        start = table.cursor_row
        await pilot.press("j")
        assert table.cursor_row == start + 1
        await pilot.press("k")
        assert table.cursor_row == start


async def test_gt_switches_tabs(tmp_path):
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g", "t")
        assert app.query_one("TabbedContent").active == "activity"


async def test_no_single_keystroke_mutates_state(tmp_path):
    """The safety model: mutations require typing a : command. If any bare key
    pins, snoozes, archives or closes, that model is broken."""
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        for key in "abcdefhilmnopqrstuvwxyzADPRSXZ":
            if key == "q":
                continue                     # quit is allowed and would end the app
            await pilot.press(key)
        assert app.home.load_state() == {}
        assert app.client.closed == []


async def test_colon_opens_the_command_line(tmp_path):
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(":")
        assert app.query_one("#command").display is True


async def test_motions_still_work_after_running_a_command(tmp_path):
    """Focus must return to the table after `:`. Testing interactions one at a
    time misses bugs that only appear in sequence — this one leaves nothing
    focused, so every motion silently dies after the first command."""
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(":")
        await pilot.press("h", "e", "l", "p", "enter")
        await pilot.pause()
        table = app.query_one("#fleet-table")
        start = table.cursor_row
        await pilot.press("j")
        assert table.cursor_row == start + 1


async def test_the_refresh_timer_does_not_move_the_selection(tmp_path):
    """The user selects a pane, pauses to type a command, and the 2s timer fires.
    Without preserved selection the cursor snaps to row 0 and `:archive` would
    close a different session than the one displayed."""
    app = app_for_many(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#fleet-table")
        await pilot.press("j")
        before = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        app.refresh_data()
        await pilot.pause()
        after = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        assert after == before


async def test_selection_survives_rows_being_reordered(tmp_path):
    """Restoring by row key rather than index is what makes this work — fleet_rows
    sorts by time-to-reap, so rows genuinely move between refreshes."""
    app = app_for_many(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#fleet-table")
        await pilot.press("j")
        chosen = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        app.client.panes = list(reversed(app.client.panes))
        app.refresh_data()
        await pilot.pause()
        assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value == chosen
