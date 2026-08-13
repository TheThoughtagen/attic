import json

import pytest

from attic.catalog import format_list, load_manifests, resolve_id
from attic.store import AtticHome


def make_archive(home, archive_id, title, archived_at):
    path = home.archive_dir / archive_id
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(json.dumps({
        "id": archive_id, "title": title, "archived_at": archived_at,
        "cwd": "/tmp/repo", "session_uuid": "u-1", "workspace": "wh dev",
        "resume": "cd /tmp/repo && claude --resume u-1",
    }), encoding="utf-8")
    (path / "scrollback.txt").write_text("some output\n", encoding="utf-8")
    return path


def test_manifests_load_newest_first(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    make_archive(home, "20260101T000000Z-old", "Old", "2026-01-01T00:00:00Z")
    make_archive(home, "20260812T000000Z-new", "New", "2026-08-12T00:00:00Z")
    assert [m["title"] for m in load_manifests(home)] == ["New", "Old"]


def test_resolve_by_unique_prefix(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    make_archive(home, "20260812T000000Z-new", "New", "2026-08-12T00:00:00Z")
    assert resolve_id(home, "20260812")["title"] == "New"


def test_resolve_rejects_ambiguous_prefix(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    make_archive(home, "20260812T000000Z-a", "A", "2026-08-12T00:00:00Z")
    make_archive(home, "20260812T000000Z-b", "B", "2026-08-12T00:00:01Z")
    with pytest.raises(LookupError, match="ambiguous"):
        resolve_id(home, "20260812")


def test_resolve_rejects_unknown_id(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    with pytest.raises(LookupError, match="no archive"):
        resolve_id(home, "nope")


def test_format_list_includes_id_and_title(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    make_archive(home, "20260812T000000Z-new", "Debug the thing", "2026-08-12T00:00:00Z")
    out = format_list(load_manifests(home))
    assert "20260812T000000Z-new" in out
    assert "Debug the thing" in out


def test_format_list_handles_empty_attic():
    assert "no archives" in format_list([]).lower()


def test_list_survives_a_manifest_with_a_null_timestamp(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    make_archive(home, "20260812T000000Z-good", "Good", "2026-08-12T00:00:00Z")
    make_archive(home, "20260813T000000Z-bad", "Bad", None)
    titles = [m["title"] for m in load_manifests(home)]
    assert "Good" in titles
    assert "Bad" in titles


def test_exact_id_wins_over_a_longer_prefix_match(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    make_archive(home, "20260812T000000Z-a", "A", "2026-08-12T00:00:00Z")
    make_archive(home, "20260812T000000Z-ab", "AB", "2026-08-12T00:00:01Z")
    assert resolve_id(home, "20260812T000000Z-a")["title"] == "A"
