import pytest

pytest.importorskip("textual")

from fakes import FakeHerdrClient
from test_policy import mkpane
from textual.widgets import Footer, TabbedContent

from attic.store import AtticHome
from attic.tui.app import AtticApp

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


async def test_i_toggles_the_detail_panel(tmp_path):
    """A panel toggle reads and mutates nothing, so it is allowed on a bare key.
    test_no_single_keystroke_mutates_state already presses every letter and
    asserts no state change, which covers `i` automatically."""
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one("#detail")
        assert panel.display is False
        await pilot.press("i")
        assert panel.display is True
        await pilot.press("i")
        assert panel.display is False


async def test_the_panel_describes_the_selected_pane_and_follows_the_cursor(tmp_path):
    """The panel must track the selection the same way commands do, or it
    describes one pane while `:archive` acts on another."""
    app = app_for_many(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        table = app.query_one("#fleet-table")
        first = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        shown = str(app.query_one("#detail").render())
        assert "dir" in shown and "last ask" in shown
        # The panel names its pane, so it is verifiable that it describes the
        # SELECTED one — the fixture's panes share a title and cwd otherwise.
        assert first in shown

        await pilot.press("j")
        await pilot.pause()
        moved = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        assert moved != first, "fixture did not actually move the cursor"
        after = str(app.query_one("#detail").render())
        assert moved in after and first not in after


async def test_the_fleet_table_carries_size_and_repo(tmp_path):
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        cols = [str(c.label) for c in app.query_one("#fleet-table").columns.values()]
        assert "size" in cols and "repo" in cols


async def test_a_prompt_containing_brackets_does_not_break_the_panel(tmp_path):
    """Textual reads [...] as markup. Real prompts in this fleet contain things
    like "[Image #1]" and paths like "[/tmp/x]" — the latter raises MarkupError
    straight into a message handler, and "[b]" silently swallows surrounding
    text. The panel shows titles, paths and your own prompts, so all of it is
    escaped and rendered literally."""
    import json

    from attic.resumable import session_path

    home = AtticHome(tmp_path)
    home.ensure()
    pane = mkpane("w4:p2")
    root = tmp_path / "projects"
    path = session_path(pane.cwd, pane.session_uuid, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    nasty = "check [/tmp/x] and [b]bold[/b] and [Image #1]"
    path.write_text(
        json.dumps({"type": "user", "message": {"role": "user"}}, separators=(",", ":"))
        + "\n"
        + json.dumps({"type": "last-prompt", "lastPrompt": nasty}, separators=(",", ":"))
        + "\n", encoding="utf-8")

    app = AtticApp(home, FakeHerdrClient(panes=[pane]), projects_root=root)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        assert app.is_running, "MarkupError killed the app"
        shown = str(app.query_one("#detail").render())
        assert "[/tmp/x]" in shown and "[b]bold[/b]" in shown and "[Image #1]" in shown


async def test_the_size_column_and_the_panel_agree(tmp_path):
    """Both must resolve transcripts from the same projects root, or the column
    reads '—' while the panel reports a size for the same pane."""
    import json

    from attic.resumable import session_path

    home = AtticHome(tmp_path)
    home.ensure()
    pane = mkpane("w4:p2")
    root = tmp_path / "projects"
    path = session_path(pane.cwd, pane.session_uuid, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "user", "message": {"role": "user"}}, separators=(",", ":"))
        + "\n", encoding="utf-8")

    app = AtticApp(home, FakeHerdrClient(panes=[pane]), projects_root=root)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#fleet-table")
        # Looked up by name: a hardcoded index silently reads the wrong column
        # the moment one is inserted, which is exactly what happened here.
        cols = [str(c.label) for c in table.columns.values()]
        size_cell = str(table.get_row_at(0)[cols.index("size")])
        assert size_cell != "—", "size column ignored projects_root"
        await pilot.press("i")
        await pilot.pause()
        assert size_cell in str(app.query_one("#detail").render())


async def test_the_panel_updates_when_the_tab_changes(tmp_path):
    """The panel is Fleet-only. Switching away must say so immediately, not
    leave a stale pane description until the next 2s refresh."""
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        assert "dir" in str(app.query_one("#detail").render())
        await pilot.press("g", "t")            # -> activity
        await pilot.pause()
        assert "Fleet tab" in str(app.query_one("#detail").render())
        await pilot.press("g", "T")            # back to fleet
        await pilot.pause()
        assert "dir" in str(app.query_one("#detail").render())


def _box(widget):
    """(top, bottom) screen rows a widget occupies."""
    r = widget.region
    return r.y, r.y + r.height


async def test_the_detail_panel_is_actually_on_screen(tmp_path):
    """Asserting `display is True` did not catch this: the panel was displayed,
    sized and populated, and laid out at y=29 on a 30-row screen — entirely off
    the visible area, underneath the footer. TabbedContent expands to fill the
    screen, so a sibling appended after it has nowhere to go. Geometry is the
    only assertion that distinguishes "shown" from "visible"."""
    app = app_for(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()

        height = app.screen.size.height
        panel_top, panel_bottom = _box(app.query_one("#detail"))
        footer_top, _ = _box(app.query_one(Footer))

        assert panel_bottom <= height, "panel extends past the bottom of the screen"
        assert panel_top < panel_bottom, "panel has no height"
        assert panel_bottom <= footer_top, "panel is drawn underneath the footer"


async def test_the_panel_and_command_line_do_not_overlap(tmp_path):
    """Both live at the bottom. Docking them separately made each claim the same
    rows, so typing a command drew over the panel describing its target."""
    app = app_for(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.press(":")
        await pilot.pause()

        _, panel_bottom = _box(app.query_one("#detail"))
        cmd_top, cmd_bottom = _box(app.query_one("#command"))
        footer_top, _ = _box(app.query_one(Footer))

        assert panel_bottom <= cmd_top, "panel overlaps the command line"
        assert cmd_bottom <= footer_top, "command line is drawn under the footer"


async def test_the_tab_area_shrinks_to_make_room(tmp_path):
    """The proof that space is reserved rather than overdrawn."""
    app = app_for(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = app.query_one(TabbedContent).size.height
        await pilot.press("i")
        await pilot.pause()
        assert app.query_one(TabbedContent).size.height < before


async def test_the_fleet_table_shows_a_summary_column(tmp_path):
    """The Fleet tab is the one you scan; without the title every row was
    identified only by an opaque pane id."""
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#fleet-table")
        cols = [str(c.label) for c in table.columns.values()]
        assert "summary" in cols
        assert str(table.get_row_at(0)[cols.index("summary")]) == "Some task"


async def test_the_panel_shows_the_untruncated_summary(tmp_path):
    app = app_for(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        assert "summary   Some task" in str(app.query_one("#detail").render())


async def test_a_blank_title_falls_back_to_a_dash(tmp_path):
    """A whitespace-only title is truthy, so `title or "—"` rendered an empty
    line rather than the fallback."""
    home = AtticHome(tmp_path)
    home.ensure()
    pane = mkpane("w4:p2")
    pane = type(pane)(**{**pane.__dict__, "title": "   "})
    app = AtticApp(home, FakeHerdrClient(panes=[pane]), projects_root=tmp_path / "projects")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        assert "summary   —" in str(app.query_one("#detail").render())
