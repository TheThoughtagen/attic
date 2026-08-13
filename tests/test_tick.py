import json
from datetime import datetime, timedelta, timezone

from attic.cli import run_tick
from attic.store import AtticHome, PaneState
from fakes import FakeHerdrClient
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)


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
    result = run_tick(home, client, NOW)
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
    run_tick(home, client, NOW)
    entry = json.loads(home.index_path.read_text().strip())
    assert entry["close_failed"] is True
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
