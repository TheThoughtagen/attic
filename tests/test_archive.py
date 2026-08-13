import builtins
import json
import stat
from datetime import datetime, timedelta, timezone
from unittest import mock

from attic.archive import Archiver, make_archive_id, slugify
from attic.policy import Archive
from attic.store import AtticHome
from fakes import FakeHerdrClient
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)
IDLE_SINCE = NOW - timedelta(hours=5)


def setup(tmp_path, **kw):
    home = AtticHome(tmp_path)
    home.ensure()
    pane = mkpane("w4:p2")
    client = FakeHerdrClient(panes=[pane], labels={"w1": "wh dev"}, **kw)
    return home, client, Archive(pane, IDLE_SINCE)


def test_slugify_lowercases_and_collapses():
    assert slugify("Debug batch: transaction/group logging!") == \
        "debug-batch-transaction-group-logging"


def test_slugify_truncates_to_maxlen():
    assert len(slugify("x" * 200)) == 48


def test_slugify_handles_empty_title():
    assert slugify("") == "untitled"


def test_archive_id_shape():
    assert make_archive_id(NOW, "Some Task", set()) == "20260813T154700Z-some-task"


def test_archive_id_disambiguates_collisions():
    existing = {"20260813T154700Z-some-task"}
    assert make_archive_id(NOW, "Some Task", existing) == "20260813T154700Z-some-task-2"


def test_archive_is_owner_readable_only(tmp_path):
    """Scrollback captures whatever was on screen — echoed tokens, .env dumps,
    connection strings. Default umask would make it world-readable."""
    home, client, action = setup(tmp_path)
    path = Archiver(home, client).archive(action, "wh dev", NOW)
    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((path / "scrollback.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((path / "manifest.json").stat().st_mode) == 0o600


def test_archive_writes_scrollback_and_manifest(tmp_path):
    home, client, action = setup(tmp_path)
    path = Archiver(home, client).archive(action, "wh dev", NOW)
    assert path is not None
    assert (path / "scrollback.txt").read_text() == "line one\nline two\n"
    m = json.loads((path / "manifest.json").read_text())
    assert m["session_uuid"] == "u-1"
    assert m["pane_id"] == "w4:p2"
    assert m["terminal_id"] == "term_w4:p2"
    assert m["workspace"] == "wh dev"
    assert m["cwd"] == "/tmp/repo"
    assert m["archived_at"] == "2026-08-13T15:47:00Z"
    assert m["idle_since"] == "2026-08-13T10:47:00Z"
    assert m["scrollback_lines"] == 2
    assert m["resume"] == "cd /tmp/repo && claude --resume u-1"
    assert m["resume_argv"] == ["claude", "--resume", "u-1"]


def test_archive_sizes_read_from_pane_scroll_rows(tmp_path):
    home, client, action = setup(tmp_path)
    Archiver(home, client).archive(action, "wh dev", NOW)
    assert client.reads == [("w4:p2", 100)]


def test_failed_read_writes_nothing_and_returns_none(tmp_path):
    home, client, action = setup(tmp_path)
    client.fail_read.add("w4:p2")
    assert Archiver(home, client).archive(action, "wh dev", NOW) is None
    assert list(home.archive_dir.glob("2026*")) == []
    assert client.closed == []


def test_empty_read_is_treated_as_failure(tmp_path):
    home, client, action = setup(tmp_path)
    client.empty_read.add("w4:p2")
    assert Archiver(home, client).archive(action, "wh dev", NOW) is None
    # Same assertion as the failed-read sibling: a regression that moved the empty
    # check after the first write would otherwise slip through.
    assert list(home.archive_dir.glob("2026*")) == []
    assert client.closed == []


def test_manifest_write_failure_leaves_no_orphan_directory(tmp_path):
    """Scrollback written, manifest not: the pane correctly survives, but without
    cleanup the directory is invisible to `attic list` AND immune to prune_archives
    (both skip manifest-less dirs), so it would accumulate forever."""
    home, client, action = setup(tmp_path)
    real_open = builtins.open

    def flaky_open(path, *a, **k):
        if str(path).endswith("manifest.json"):
            raise OSError(28, "No space left on device")
        return real_open(path, *a, **k)

    with mock.patch("attic.archive.open", flaky_open, create=True):
        assert Archiver(home, client).archive(action, "wh dev", NOW) is None
    assert list(home.archive_dir.iterdir()) == []
    assert client.closed == []


def test_archive_never_closes_the_pane_itself(tmp_path):
    """Closing is the caller's job, and only on a non-None return."""
    home, client, action = setup(tmp_path)
    Archiver(home, client).archive(action, "wh dev", NOW)
    assert client.closed == []


def test_archive_survives_dense_unicode_scrollback(tmp_path):
    """Scrollback is full of box drawing, spinners and emoji. launchd starts jobs
    with no LANG, so a locale-derived encoding differs from the shell's — guessing
    wrong raises UnicodeEncodeError and every archive fails silently forever."""
    home, client, action = setup(tmp_path)
    client.scrollback = "◐ working… ╭───╮ │ ✳ │ ╰───╯ 🔧 café\n"
    path = Archiver(home, client).archive(action, "wh dev", NOW)
    assert path is not None
    assert (path / "scrollback.txt").read_text(encoding="utf-8") == client.scrollback


def test_append_index_is_one_json_object_per_line(tmp_path):
    home, client, _ = setup(tmp_path)
    arch = Archiver(home, client)
    arch.append_index({"id": "a"})
    arch.append_index({"id": "b"})
    lines = home.index_path.read_text().strip().splitlines()
    assert [json.loads(x)["id"] for x in lines] == ["a", "b"]
