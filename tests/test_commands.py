import json
from datetime import datetime, timezone

import pytest

from attic.resumable import session_path
from attic.store import AtticHome
from attic.tui.commands import CommandContext, parse_command, run_command
from fakes import FakeHerdrClient
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)


def make_resumable(root, panes):
    """The resumability gate requires a Claude transcript on disk."""
    for pane in panes:
        path = session_path(pane.cwd, pane.session_uuid, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"user"}\n', encoding="utf-8")
    return root


def ctx(tmp_path, tab="fleet", row_key="w4:p2", panes=None):
    home = AtticHome(tmp_path)
    home.ensure()
    return CommandContext(
        home=home,
        client=FakeHerdrClient(panes=panes if panes is not None else [mkpane("w4:p2")]),
        tab=tab, row_key=row_key, now=NOW, projects_root=tmp_path / "projects")


def test_parse_splits_verb_and_arguments():
    assert parse_command(":snooze 4h") == ("snooze", ["4h"])
    assert parse_command("pin") == ("pin", [])


def test_parse_rejects_an_unknown_verb():
    with pytest.raises(ValueError, match="unknown command"):
        parse_command(":frobnicate")


def test_pin_stores_under_the_terminal_id(tmp_path):
    c = ctx(tmp_path)
    assert run_command("pin", [], c).ok
    assert c.home.load_state()["term_w4:p2"].pinned is True


def test_snooze_requires_a_duration(tmp_path):
    """Defaulting would silently pick a protection window the user did not choose."""
    result = run_command("snooze", [], ctx(tmp_path))
    assert not result.ok
    assert "duration" in result.message


def test_snooze_reports_the_previous_deadline(tmp_path):
    c = ctx(tmp_path)
    run_command("snooze", ["8h"], c)
    result = run_command("snooze", ["1h"], c)
    assert "was" in result.message


def test_archive_refuses_when_the_session_is_not_resumable(tmp_path):
    """Manual archive skips the threshold, never the recoverability check."""
    c = ctx(tmp_path)
    (tmp_path / "projects").mkdir()
    result = run_command("archive", [], c)
    assert not result.ok
    assert "claude --resume would fail" in result.message
    assert c.client.closed == []


def test_a_command_that_does_not_apply_to_this_tab_says_so(tmp_path):
    result = run_command("restore", [], ctx(tmp_path, tab="fleet"))
    assert not result.ok
    assert "Attic" in result.message


def test_pin_on_the_attic_tab_says_so(tmp_path):
    result = run_command("pin", [], ctx(tmp_path, tab="attic"))
    assert not result.ok
    assert "Fleet" in result.message


def test_a_command_with_no_selected_row_fails_cleanly(tmp_path):
    result = run_command("pin", [], ctx(tmp_path, row_key=None))
    assert not result.ok
    assert "no row" in result.message


def test_archive_via_the_tui_leaves_an_index_entry(tmp_path):
    """run_tick and the TUI's :archive share archive_and_close precisely so this
    can't drift — a TUI archive with no index entry would leave no audit trail
    and no close_failed marker, unlike every archive the daemon performs."""
    pane = mkpane("w4:p2")
    c = ctx(tmp_path, panes=[pane])
    make_resumable(tmp_path / "projects", [pane])
    result = run_command("archive", [], c)
    assert result.ok
    assert c.client.closed == ["w4:p2"]
    entry = json.loads(c.home.index_path.read_text().strip())
    assert entry["pane_id"] == "w4:p2"
    assert entry["close_failed"] is False


def test_surplus_arguments_are_rejected_not_ignored(tmp_path):
    """`:archive typo` must not archive. Accepting stray words would mean the
    command that runs is not the command that was typed — the same class of
    defect as a refresh retargeting a command onto another pane."""
    for text in (":archive typo", ":pin now", ":unpin x", ":restore abc def"):
        with pytest.raises(ValueError, match="argument"):
            parse_command(text)


def test_snooze_still_requires_exactly_one_duration(tmp_path):
    verb, args = parse_command(":snooze 4h")
    assert (verb, args) == ("snooze", ["4h"])
    for bad in (":snooze", ":snooze 4h 5h"):
        with pytest.raises(ValueError, match="argument"):
            parse_command(bad)
