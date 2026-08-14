import json
from datetime import UTC, datetime

from test_policy import mkpane

from attic.evaluate import Evaluation
from attic.policy import Archive, Skip
from attic.rows import activity_rows, attic_rows, fleet_rows, humanize
from attic.store import AtticHome, Config, PaneState

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=UTC)


def test_humanize_reads_naturally():
    assert humanize(0) == "0m"
    assert humanize(60 * 42) == "42m"
    assert humanize(3600 * 4 + 60 * 12) == "4h 12m"
    assert humanize(86400 * 3 + 3600) == "3d 1h"
    assert humanize(None) == "—"


def test_fleet_rows_carry_the_verdict_and_reason():
    pane = mkpane("w4:p2")
    ev = Evaluation(panes=[pane], state={}, labels={"w1": "wh dev"},
                    actions=[Skip(pane, "pinned")], config=Config())
    row = fleet_rows(ev, NOW)[0]
    assert row.pane_id == "w4:p2"
    assert row.workspace == "wh dev"
    assert row.verdict == "skip"
    assert row.reason == "pinned"
    assert row.terminal_id == pane.terminal_id


def test_fleet_rows_show_idle_duration_from_state():
    pane = mkpane("w4:p2")
    state = {pane.terminal_id: PaneState("2026-08-13T11:35:00Z", 1)}
    ev = Evaluation(panes=[pane], state=state, labels={},
                    actions=[Skip(pane, "not idle long enough")], config=Config())
    assert fleet_rows(ev, NOW)[0].idle_for == "4h 12m"


def test_fleet_rows_sort_archives_first_then_by_idle_time():
    """Sorted by how close each pane is to being reaped, so the thing about to
    happen is at the top where it will be seen."""
    a, b, c = mkpane("w1:p1"), mkpane("w1:p2"), mkpane("w1:p3")
    state = {
        a.terminal_id: PaneState("2026-08-13T14:47:00Z", 1),   # 1h idle
        b.terminal_id: PaneState("2026-08-13T05:47:00Z", 1),   # 10h idle
        c.terminal_id: PaneState("2026-08-13T13:47:00Z", 1),   # 2h idle
    }
    ev = Evaluation(panes=[a, b, c], state=state, labels={},
                    actions=[Skip(a, "not idle long enough"), Archive(b, NOW),
                             Skip(c, "not idle long enough")], config=Config())
    assert [r.pane_id for r in fleet_rows(ev, NOW)] == ["w1:p2", "w1:p3", "w1:p1"]


def test_activity_rows_read_the_inventory_newest_first(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    path = home.inventory_dir / "2026-08-13.jsonl"
    path.write_text(
        json.dumps({"at": "2026-08-13T10:00:00Z", "panes": [
            {"pane_id": "w1:p1", "title": "Older", "verdict": "skip", "reason": "pinned"}]})
        + "\n" +
        json.dumps({"at": "2026-08-13T11:00:00Z", "panes": [
            {"pane_id": "w1:p2", "title": "Newer", "verdict": "archive", "reason": ""}]})
        + "\n", encoding="utf-8")
    rows = activity_rows(home)
    assert [r.title for r in rows] == ["Newer", "Older"]
    assert rows[0].verdict == "archive"


def test_activity_rows_tolerate_lines_written_before_verdicts_existed(tmp_path):
    """Inventory lines predating the verdict field must not break the view."""
    home = AtticHome(tmp_path)
    home.ensure()
    (home.inventory_dir / "2026-08-13.jsonl").write_text(
        json.dumps({"at": "2026-08-13T10:00:00Z",
                    "panes": [{"pane_id": "w1:p1", "title": "Legacy"}]}) + "\n",
        encoding="utf-8")
    row = activity_rows(home)[0]
    assert row.verdict == "—"
    assert row.reason == ""


def test_activity_rows_skip_a_corrupt_line_without_losing_the_others(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    (home.inventory_dir / "2026-08-13.jsonl").write_text(
        "{not json\n" +
        json.dumps({"at": "2026-08-13T10:00:00Z", "panes": [
            {"pane_id": "w1:p1", "title": "Good", "verdict": "skip", "reason": "x"}]}) + "\n",
        encoding="utf-8")
    assert [r.title for r in activity_rows(home)] == ["Good"]


def test_activity_rows_survive_a_non_list_panes_field(tmp_path):
    """`{"panes": 5}` parses as valid JSON and then raises at the loop. One bad
    line must not destroy the view — it is what someone reads when they are
    already trying to work out what went wrong."""
    home = AtticHome(tmp_path)
    home.ensure()
    (home.inventory_dir / "2026-08-13.jsonl").write_text(
        json.dumps({"at": "2026-08-13T09:00:00Z", "panes": 5}) + "\n" +
        json.dumps({"at": "2026-08-13T10:00:00Z", "panes": [
            {"pane_id": "w1:p1", "title": "Good", "verdict": "skip", "reason": "x"}]}) + "\n",
        encoding="utf-8")
    assert [r.title for r in activity_rows(home)] == ["Good"]


def test_attic_rows_are_newest_first(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    for name, stamp, title in [("20260101T000000Z-old", "2026-01-01T00:00:00Z", "Old"),
                               ("20260812T000000Z-new", "2026-08-12T00:00:00Z", "New")]:
        d = home.archive_dir / name
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(
            {"id": name, "archived_at": stamp, "title": title, "workspace": "wh dev"}),
            encoding="utf-8")
    assert [r.title for r in attic_rows(home)] == ["New", "Old"]


def test_ellipsize_trims_on_a_word_boundary():
    from attic.rows import ellipsize
    assert ellipsize("short", 20) == "short"
    assert ellipsize("", 20) == ""
    long = "Review ignition security diagnostics phase B handoff"
    out = ellipsize(long, 30)
    assert len(out) <= 30
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")
    # a single unbroken token still gets cut rather than overflowing the column
    assert len(ellipsize("x" * 100, 12)) <= 12


def test_fleet_rows_carry_the_pane_title_as_a_summary():
    """herdr's pane title is Claude Code's own ai-title. It was shown on the
    Activity and Attic tabs but never on Fleet, which is the tab you scan."""
    pane = mkpane("w1:p1")
    ev = Evaluation(panes=[pane], state={}, labels={},
                    actions=[Skip(pane, "pinned")], config=Config())
    row = fleet_rows(ev, NOW)[0]
    assert row.summary == pane.title


def test_a_blank_title_becomes_a_dash_in_the_fleet_row():
    """The table and the detail panel must agree. A whitespace-only title is
    truthy, so the naive fallback rendered an empty cell in one and a dash in
    the other for the same pane."""
    for blank in ("", "   ", "\t\n"):
        pane = mkpane("w1:p1")
        pane = type(pane)(**{**pane.__dict__, "title": blank})
        ev = Evaluation(panes=[pane], state={}, labels={},
                        actions=[Skip(pane, "pinned")], config=Config())
        assert fleet_rows(ev, NOW)[0].summary == "—"
