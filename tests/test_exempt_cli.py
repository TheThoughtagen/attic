import json

from attic.cli import main
from attic.store import AtticHome
from fakes import FakeHerdrClient
from test_policy import mkpane


def stub_client(monkeypatch, panes):
    monkeypatch.setattr("attic.cli.HerdrClient", lambda: FakeHerdrClient(panes=panes))


def test_pin_stores_under_the_terminal_id(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    stub_client(monkeypatch, [mkpane("w4:p2", terminal_id="term_abc")])
    assert main(["pin", "w4:p2"]) == 0
    assert AtticHome(tmp_path).load_state()["term_abc"].pinned is True
    assert "pinned" in capsys.readouterr().out


def test_snooze_reports_both_deadlines_when_replacing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    stub_client(monkeypatch, [mkpane("w4:p2", terminal_id="term_abc")])
    main(["snooze", "w4:p2", "8h"])
    capsys.readouterr()
    assert main(["snooze", "w4:p2", "1h"]) == 0
    out = capsys.readouterr().out
    assert "was" in out          # the shortening is reported, not silent


def test_snooze_on_a_pinned_pane_says_it_has_no_effect(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    stub_client(monkeypatch, [mkpane("w4:p2", terminal_id="term_abc")])
    main(["pin", "w4:p2"])
    capsys.readouterr()
    main(["snooze", "w4:p2", "4h"])
    assert "pinned" in capsys.readouterr().out


def test_bad_duration_exits_nonzero_without_changing_state(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    stub_client(monkeypatch, [mkpane("w4:p2", terminal_id="term_abc")])
    assert main(["snooze", "w4:p2", "4w"]) == 1
    assert AtticHome(tmp_path).load_state() == {}
    assert "30m" in capsys.readouterr().err


def test_unknown_pane_exits_nonzero(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    stub_client(monkeypatch, [mkpane("w4:p2", terminal_id="term_abc")])
    assert main(["pin", "w9:p9"]) == 1
    assert "w9:p9" in capsys.readouterr().err


def test_existing_commands_still_work(monkeypatch, tmp_path, capsys):
    """Guard against clobbering main() while appending subcommands."""
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    assert main(["list"]) == 0
    assert "no archives" in capsys.readouterr().out
