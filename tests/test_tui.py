import pytest

pytest.importorskip("textual")

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
    import string

    app = app_for(tmp_path)
    # Derived, not hardcoded: a future single-letter mutating Binding is
    # automatically covered rather than silently missed by a stale key list.
    bound = {b.key for b in AtticApp.BINDINGS if len(b.key) == 1}
    async with app.run_test() as pilot:
        await pilot.pause()
        for key in sorted(set(string.ascii_letters) | bound):
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


async def test_a_refresh_while_typing_cannot_retarget_the_command(tmp_path):
    """The Critical: a 2s timer must not redirect `:pin` onto a pane the user
    did not select. This asserts the command acts on the row chosen at `:`-time,
    even though refresh_data() is suspended while the command line is open — the
    timer firing mid-typing must not silently retarget or silently do nothing
    wrong either."""
    app = app_for_many(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#fleet-table")
        await pilot.press("j")
        chosen = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        await pilot.press(":")
        app.refresh_data()                      # the timer fires mid-typing
        await pilot.pause()
        await pilot.press("p", "i", "n", "enter")
        await pilot.pause()
        pinned = [k for k, v in app.home.load_state().items() if v.pinned]
        assert pinned == [f"term_{chosen}"]


async def test_captured_target_survives_the_row_disappearing(tmp_path):
    """Isolates C1(a) specifically. With the command line open, refresh_data()
    is suspended (C1(b)), so this test bypasses that guard directly and
    repopulates the table itself — simulating what a future regression that
    removes the C1(b) guard would let through — to prove the row-key captured
    at `:`-open time, not the cursor read at Enter, is what decides the target.
    Without that capture, a vanished row's slot would be filled by whatever
    pane now sorts into it, and the command would silently act on that pane
    instead of refusing."""
    app = app_for_many(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#fleet-table")
        await pilot.press("j")
        chosen = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        await pilot.press(":")
        app.client.panes = [p for p in app.client.panes if p.pane_id != chosen]
        table.clear()
        for pane in app.client.panes:
            table.add_row(pane.pane_id, "", "", "", "", "", key=pane.pane_id)
        table.move_cursor(row=0)
        await pilot.press("p", "i", "n", "enter")
        await pilot.pause()
        # The vanished pane must be refused, not silently swapped for row 0's pane.
        assert app.home.load_state() == {}


async def test_herdr_dying_mid_command_does_not_crash_the_tui(tmp_path):
    """run_command's pane_list() call can raise HerdrError — on_input_submitted
    must catch it the same way refresh_data already catches herdr failures,
    rather than letting it propagate and kill the app."""
    from attic.herdr import HerdrError

    class Dead:
        def pane_list(self):
            raise HerdrError("socket gone")

    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.client = Dead()
        await pilot.press("j")
        await pilot.press(":")
        await pilot.press("p", "i", "n", "enter")
        await pilot.pause()
        assert app.is_running
        assert app.home.load_state() == {}


async def test_tui_archive_goes_through_the_shared_execution_path(tmp_path):
    """The keystroke path: typing `:archive` reaches archive_and_close intact.

    test_archive_via_the_tui_leaves_an_index_entry already covers the execution
    half by calling run_command directly. This covers the seam BEFORE that call —
    keypress, command parse, captured row key, dispatch — which is exactly where
    the branch's worst defect lived: every component was individually correct and
    the command still reached the wrong pane. That seam has no other end-to-end
    test, so it gets one for the destructive command specifically.
    """
    import json

    from attic.resumable import session_path

    home = AtticHome(tmp_path)
    home.ensure()
    panes = [mkpane("w4:p2"), mkpane("w4:p3")]
    client = FakeHerdrClient(panes=panes)
    root = tmp_path / "projects"
    for pane in panes:  # resume_blocker refuses to close an unwritten transcript
        path = session_path(pane.cwd, pane.session_uuid, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"user"}\n', encoding="utf-8")

    app = AtticApp(home, client, projects_root=root)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#fleet-table")
        chosen = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        await pilot.press(":")
        await pilot.press("a", "r", "c", "h", "i", "v", "e", "enter")
        await pilot.pause()

    assert client.closed == [chosen]
    entries = [json.loads(line) for line in home.index_path.read_text().splitlines()]
    assert [e["pane_id"] for e in entries] == [chosen]
    assert entries[0]["close_failed"] is False
