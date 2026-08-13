import json
import os
from datetime import datetime, timedelta, timezone

from attic.inventory import append_inventory, prune_archives, prune_inventory
from attic.store import AtticHome
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)


def test_inventory_line_records_every_pane(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    path = append_inventory(home, [mkpane("w4:p2")], {"w1": "wh dev"}, NOW)
    assert path.name == "2026-08-13.jsonl"
    entry = json.loads(path.read_text().strip())
    assert entry["at"] == "2026-08-13T15:47:00Z"
    assert entry["panes"][0] == {
        "pane_id": "w4:p2", "workspace": "wh dev", "cwd": "/tmp/repo",
        "title": "Some task", "status": "idle", "session_uuid": "u-1",
    }


def test_inventory_appends_within_the_same_day(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    append_inventory(home, [mkpane()], {}, NOW)
    path = append_inventory(home, [mkpane()], {}, NOW)
    assert len(path.read_text().strip().splitlines()) == 2


def test_prune_inventory_removes_files_past_retention(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    (home.inventory_dir / "2026-01-01.jsonl").write_text("{}\n")   # 224 days old
    (home.inventory_dir / "2026-08-12.jsonl").write_text("{}\n")   # 1 day old
    removed = prune_inventory(home, NOW, retention_days=90)
    assert [p.name for p in removed] == ["2026-01-01.jsonl"]
    assert (home.inventory_dir / "2026-08-12.jsonl").exists()


def test_prune_inventory_ignores_unparseable_names(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    (home.inventory_dir / "notes.txt").write_text("x")
    assert prune_inventory(home, NOW, retention_days=1) == []
    assert (home.inventory_dir / "notes.txt").exists()


def test_prune_archives_uses_manifest_archived_at(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    old = home.archive_dir / "20260101T000000Z-old"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps(
        {"archived_at": "2026-01-01T00:00:00Z", "title": "Old task"}))
    fresh = home.archive_dir / "20260812T000000Z-fresh"
    fresh.mkdir()
    (fresh / "manifest.json").write_text(json.dumps(
        {"archived_at": "2026-08-12T00:00:00Z", "title": "Fresh task"}))
    removed = prune_archives(home, NOW, retention_days=30)
    assert [p.name for p in removed] == ["20260101T000000Z-old"]
    assert not old.exists()
    assert fresh.exists()


def test_prune_archives_spares_recent_dirs_without_manifest(tmp_path):
    """A freshly-created manifest-less dir may be an in-flight write. Never touch it."""
    home = AtticHome(tmp_path)
    home.ensure()
    (home.archive_dir / "20260101T000000Z-broken").mkdir()
    assert prune_archives(home, NOW, retention_days=1) == []
    assert (home.archive_dir / "20260101T000000Z-broken").exists()


def test_malformed_manifests_never_raise_and_fall_back_to_mtime(tmp_path):
    """A manifest we cannot trust is treated as no manifest: mtime-gated, never fatal.
    prune_archives is the only irreversible operation here and it runs unattended —
    an exception mid-loop aborts every remaining directory in that run."""
    home = AtticHome(tmp_path)
    home.ensure()
    payloads = {
        "a-list": "[]",
        "b-string": '"x"',
        "c-number": "42",
        "d-nonstring-stamp": '{"archived_at": 12345}',
        "e-unparseable-stamp": '{"archived_at": "not a date"}',
        "f-naive-stamp": '{"archived_at": "2026-01-01T00:00:00"}',
        "g-missing-key": "{}",
        "h-not-json": "{not json",
    }
    for name, payload in payloads.items():
        d = home.archive_dir / f"20260101T000000Z-{name}"
        d.mkdir()
        (d / "manifest.json").write_text(payload, encoding="utf-8")
        stamp = (NOW - timedelta(days=200)).timestamp()
        os.utime(d, (stamp, stamp))
    removed = prune_archives(home, NOW, retention_days=30)
    assert len(removed) == len(payloads)
    assert list(home.archive_dir.iterdir()) == []


def test_malformed_manifest_within_retention_is_spared(tmp_path):
    """An untrustworthy manifest must not shorten a directory's life."""
    home = AtticHome(tmp_path)
    home.ensure()
    d = home.archive_dir / "20260812T000000Z-bad"
    d.mkdir()
    (d / "manifest.json").write_text("[]", encoding="utf-8")
    assert prune_archives(home, NOW, retention_days=30) == []
    assert d.exists()


def test_prune_reclaims_manifest_less_dirs_past_retention(tmp_path):
    """Orphaned partial archives are invisible to `attic list`, so without this they
    would be immortal — accumulating forever in a tool built to reclaim resources."""
    home = AtticHome(tmp_path)
    home.ensure()
    old = home.archive_dir / "20260101T000000Z-orphan"
    old.mkdir()
    (old / "scrollback.txt").write_text("partial write, no manifest\n", encoding="utf-8")
    os.utime(old, (0, (NOW - timedelta(days=200)).timestamp()))
    fresh = home.archive_dir / "20260812T000000Z-orphan"
    fresh.mkdir()
    (fresh / "scrollback.txt").write_text("partial write, no manifest\n", encoding="utf-8")
    removed = prune_archives(home, NOW, retention_days=30)
    assert [p.name for p in removed] == ["20260101T000000Z-orphan"]
    assert not old.exists()
    assert fresh.exists()
