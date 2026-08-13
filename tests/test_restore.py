import json
from datetime import datetime, timezone

import pytest

from attic.restore import restore
from attic.store import AtticHome
from fakes import FakeHerdrClient

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)


def manifest(cwd: str) -> dict:
    return {
        "id": "20260812T000000Z-debug", "title": "Debug the thing",
        "cwd": cwd, "session_uuid": "u-1", "workspace": "wh dev",
        "resume": "cd X && claude --resume u-1",
        "resume_argv": ["claude", "--resume", "u-1"],
    }


def test_restore_runs_the_argv_recorded_at_archive_time(tmp_path):
    """Not one rebuilt from today's code: if the agent CLI's flags change, an old
    archive must still replay what actually worked when it was written."""
    m = manifest(str(tmp_path))
    m["resume_argv"] = ["claude", "--continue-session", "u-1"]   # a future flag
    client = FakeHerdrClient()
    restore(AtticHome(tmp_path), client, m, NOW)
    assert client.ran == [("w9:p9", ["claude", "--continue-session", "u-1"])]


def test_restore_falls_back_when_resume_argv_is_absent(tmp_path):
    """Manifests written before resume_argv existed still restore."""
    m = manifest(str(tmp_path))
    del m["resume_argv"]
    client = FakeHerdrClient()
    restore(AtticHome(tmp_path), client, m, NOW)
    assert client.ran == [("w9:p9", ["claude", "--resume", "u-1"])]


def test_restore_creates_a_tab_and_runs_the_stored_resume(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    client = FakeHerdrClient()
    pane_id = restore(home, client, manifest(str(tmp_path)), NOW)
    assert pane_id == "w9:p9"
    assert client.created_tabs == [(str(tmp_path), "Debug the thing")]
    assert client.ran == [("w9:p9", ["claude", "--resume", "u-1"])]


def test_restore_aborts_when_cwd_is_gone(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    client = FakeHerdrClient()
    with pytest.raises(FileNotFoundError):
        restore(home, client, manifest(str(tmp_path / "vanished")), NOW)
    assert client.created_tabs == []


def test_restore_is_non_destructive_and_logs_restored_at(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    archive = home.archive_dir / "20260812T000000Z-debug"
    archive.mkdir(parents=True)
    restore(home, FakeHerdrClient(), manifest(str(tmp_path)), NOW)
    assert archive.exists()
    entry = json.loads(home.index_path.read_text().strip())
    assert entry["restored_at"] == "2026-08-13T15:47:00Z"
    assert entry["id"] == "20260812T000000Z-debug"


def test_restore_twice_yields_two_panes(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    client = FakeHerdrClient()
    restore(home, client, manifest(str(tmp_path)), NOW)
    restore(home, client, manifest(str(tmp_path)), NOW)
    assert len(client.created_tabs) == 2
