import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from attic.archive import Archiver
from attic.cli import _print_verdicts, main, run_tick
from attic.store import AtticHome, PaneState
from fakes import FakeHerdrClient
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)


def make_resumable(root, panes):
    """The resumability gate requires a Claude transcript on disk. These tests
    use fake panes, so create one per pane under an isolated projects root."""
    from attic.resumable import session_path
    for pane in panes:
        path = session_path(pane.cwd, pane.session_uuid, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"user"}\n', encoding="utf-8")
    return root


def home_with_clock(tmp_path, panes, hours_idle=10):
    home = AtticHome(tmp_path)
    home.ensure()
    home.save_state({
        p.terminal_id: PaneState(
            (NOW - timedelta(hours=hours_idle)).isoformat().replace("+00:00", "Z"),
            p.revision)
        for p in panes
    })
    return home


def test_tick_archives_then_closes_in_that_order(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane], labels={"w1": "wh dev"})
    root = make_resumable(tmp_path / "projects", [pane])
    result = run_tick(home, client, NOW, projects_root=root)
    assert result.reaped is True
    assert client.closed == ["w4:p2"]
    archive_dir = next(home.archive_dir.glob("2026*"))
    assert (archive_dir / "manifest.json").exists()
    assert json.loads(home.index_path.read_text().strip())["id"] == archive_dir.name


def test_tick_always_writes_inventory(tmp_path):
    pane = mkpane("w4:p2", status="working")
    home = AtticHome(tmp_path)
    home.ensure()
    run_tick(home, FakeHerdrClient(panes=[pane]), NOW)
    assert (home.inventory_dir / "2026-08-13.jsonl").exists()


def test_pause_file_blocks_reaping_but_not_inventory(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    home.pause_path.touch()
    client = FakeHerdrClient(panes=[pane])
    result = run_tick(home, client, NOW)
    assert result.reaped is False
    assert result.reason == "paused"
    assert client.closed == []
    assert (home.inventory_dir / "2026-08-13.jsonl").exists()


def test_inventory_is_written_even_when_reaping_is_paused(tmp_path):
    """Snapshotting is observation, not action — the pause guard must not skip it."""
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    home.pause_path.touch()
    run_tick(home, FakeHerdrClient(panes=[pane]), NOW)
    line = json.loads((home.inventory_dir / "2026-08-13.jsonl").read_text(encoding="utf-8").strip())
    assert line["panes"][0]["verdict"] == "skip"


def test_dry_run_shows_verdicts_even_while_paused(tmp_path):
    """The soak depends on this. `attic` installs PAUSED, and the user reads
    `attic reap --dry-run` for days before granting reaping authority by removing
    PAUSE. If the pause guard short-circuited before decide(), that output would be
    empty and the entire trust-building procedure would be impossible to perform."""
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    home.pause_path.touch()
    client = FakeHerdrClient(panes=[pane])
    result = run_tick(home, client, NOW, dry_run=True)
    assert len(result.actions) == 1
    assert result.reason == "paused"
    assert client.closed == []
    assert list(home.archive_dir.glob("2026*")) == []


def test_paused_tick_still_reports_what_it_would_have_done(tmp_path):
    """A paused tick computes verdicts so the log can say what it declined to do."""
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    home.pause_path.touch()
    client = FakeHerdrClient(panes=[pane])
    result = run_tick(home, client, NOW)
    assert result.reaped is False
    assert result.reason == "paused"
    assert len(result.actions) == 1
    assert client.closed == []


def test_idle_clock_advances_while_paused(tmp_path):
    """Guards gate execution, never observation. If the clock froze during a pause,
    dry-run durations during the soak would bear no relation to reality."""
    pane = mkpane("w4:p2")
    home = AtticHome(tmp_path)
    home.ensure()
    home.pause_path.touch()
    run_tick(home, FakeHerdrClient(panes=[pane]), NOW)
    assert home.load_state()[pane.terminal_id].first_idle_at == "2026-08-13T15:47:00Z"


def test_protocol_mismatch_blocks_reaping(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane], protocol=20)
    result = run_tick(home, client, NOW)
    assert result.reaped is False
    assert "protocol" in result.reason
    assert client.closed == []


def test_dry_run_produces_verdicts_but_closes_nothing(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane])
    result = run_tick(home, client, NOW, dry_run=True)
    assert client.closed == []
    assert list(home.archive_dir.glob("2026*")) == []
    assert len(result.actions) == 1


def test_failed_read_leaves_pane_alive_and_unindexed(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane])
    client.fail_read.add("w4:p2")
    run_tick(home, client, NOW)
    assert client.closed == []
    assert not home.index_path.exists()


def test_close_failure_keeps_archive_and_marks_it(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane])
    client.fail_close.add("w4:p2")
    run_tick(home, client, NOW, projects_root=make_resumable(tmp_path / "projects", [pane]))
    entry = json.loads(home.index_path.read_text().strip())
    assert entry["close_failed"] is True
    assert next(home.archive_dir.glob("2026*")).exists()


def test_dry_run_output_states_why_reaping_is_disabled(capsys, tmp_path):
    """The soak has the user reading this for days while attic is PAUSED. Without
    the banner, a paused run looks identical to a run with nothing to do."""
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    home.pause_path.touch()
    result = run_tick(home, FakeHerdrClient(panes=[pane]), NOW, dry_run=True,
                      projects_root=make_resumable(tmp_path / "projects", [pane]))
    _print_verdicts(result)
    out = capsys.readouterr().out
    assert "reaping disabled: paused" in out
    assert "ARCHIVE" in out


def test_main_returns_one_for_a_nonexistent_restore(monkeypatch, tmp_path):
    """The daemon must never die, but the CLI must stop lying: every command
    previously returned 0, including error branches that print a message and
    then claim success."""
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    assert main(["restore", "nonexistent-id"]) == 1


def test_main_returns_zero_when_tick_raises(monkeypatch, tmp_path):
    """tick and reap must keep returning 0 on every path — that is what keeps the
    LaunchAgent alive even when run_tick blows up unexpectedly."""
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("attic.cli.run_tick", boom)
    assert main(["tick"]) == 0


def test_main_returns_zero_when_setup_itself_fails(monkeypatch, capsys):
    """A crashing timer stops protecting the user, and under launchd the crash
    produces no visible symptom. Even an unwritable ATTIC_HOME must exit 0."""
    def boom():
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("attic.cli.AtticHome.default", staticmethod(boom))
    assert main(["tick"]) == 0


def test_index_append_failure_after_close_does_not_propagate(tmp_path):
    """The archive and manifest are already durable and `attic list` reads manifests
    from disk, so the session stays restorable; only the index loses an entry."""
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane])
    with mock.patch.object(Archiver, "append_index", side_effect=OSError(28, "No space")):
        result = run_tick(home, client, NOW, projects_root=make_resumable(tmp_path / "projects", [pane]))
    assert result.reaped is True
    assert client.closed == ["w4:p2"]
    assert next(home.archive_dir.glob("2026*")).exists()


def test_herdr_unavailable_is_survivable(tmp_path):
    class Dead(FakeHerdrClient):
        def pane_list(self):
            from attic.herdr import HerdrError
            raise HerdrError("socket gone")

    home = AtticHome(tmp_path)
    home.ensure()
    result = run_tick(home, Dead(), NOW)
    assert result.reaped is False
    assert "herdr" in result.reason.lower()


def test_state_is_persisted_across_ticks(tmp_path):
    pane = mkpane("w4:p2")
    home = AtticHome(tmp_path)
    home.ensure()
    run_tick(home, FakeHerdrClient(panes=[pane]), NOW)
    assert home.load_state()[pane.terminal_id].first_idle_at == "2026-08-13T15:47:00Z"


def test_tick_refuses_to_close_a_pane_whose_session_is_not_yet_resumable(tmp_path):
    """The bug staging found: attic closed a real pane and then `claude --resume`
    answered "No conversation found". herdr's UUID is genuine, but Claude Code
    writes the transcript lazily, so a young session cannot be brought back.
    Closing it produces an archive that restores into an empty prompt."""
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane])
    empty_root = tmp_path / "projects"          # no transcript on disk
    empty_root.mkdir()
    result = run_tick(home, client, NOW, projects_root=empty_root)
    assert client.closed == []                                  # pane survives
    assert list(home.archive_dir.glob("2026*")) == []            # nothing archived
    reason = next(a.reason for a in result.actions if a.pane.pane_id == "w4:p2")
    assert "claude --resume would fail" in reason
