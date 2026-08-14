from datetime import datetime, timezone

import pytest

from attic.store import AtticHome
from attic.tui.commands import CommandContext, parse_command, run_command
from fakes import FakeHerdrClient
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)


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
