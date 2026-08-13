import json
from datetime import datetime, timezone

import pytest

from attic.cli import main
from attic.herdr import HerdrError
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


def test_restore_names_the_pane_when_the_session_fails_to_start(tmp_path):
    """A tab exists but nothing runs in it. The error must name the pane, or the user
    is left with a stray tab they cannot account for and no sign a restore failed."""
    home = AtticHome(tmp_path)
    home.ensure()
    client = FakeHerdrClient()
    client.fail_run = True
    with pytest.raises(HerdrError, match="w9:p9"):
        restore(home, client, manifest(str(tmp_path)), NOW)
    assert client.created_tabs                 # the tab really does exist
    assert not home.index_path.exists()        # but nothing claims it was restored


def test_restore_prints_the_manifest_when_cwd_is_gone(monkeypatch, capsys, tmp_path):
    """Aborting is right, but the user needs to see WHAT was abandoned — especially
    the resume string, which lets them recover by hand if the directory merely moved."""
    home = AtticHome(tmp_path)
    home.ensure()
    data = {"id": "20260812T000000Z-x", "title": "T", "cwd": "/nonexistent/path",
            "session_uuid": "u-1", "archived_at": "2026-08-12T00:00:00Z",
            "resume": "cd /nonexistent/path && claude --resume u-1"}
    d = home.archive_dir / data["id"]
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    assert main(["restore", "20260812T000000Z-x"]) == 0
    err = capsys.readouterr().err
    assert "cwd no longer exists" in err
    assert "u-1" in err            # the manifest itself was shown, not just the message


def test_restore_twice_yields_two_panes(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    client = FakeHerdrClient()
    restore(home, client, manifest(str(tmp_path)), NOW)
    restore(home, client, manifest(str(tmp_path)), NOW)
    assert len(client.created_tabs) == 2
