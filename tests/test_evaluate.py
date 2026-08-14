from datetime import UTC, datetime, timedelta

from fakes import FakeHerdrClient
from test_policy import mkpane

from attic.evaluate import Evaluation, evaluate, gate_on_resumability
from attic.policy import Archive, Skip
from attic.store import AtticHome, PaneState

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=UTC)


def home_with_clock(tmp_path, panes, hours=9):
    home = AtticHome(tmp_path)
    home.ensure()
    home.save_state({
        p.terminal_id: PaneState(
            (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"), p.revision)
        for p in panes
    })
    return home


def transcripts_for(root, panes):
    from attic.resumable import session_path
    for pane in panes:
        path = session_path(pane.cwd, pane.session_uuid, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"user"}\n', encoding="utf-8")
    return root


def test_evaluate_returns_panes_state_and_gated_actions(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    root = transcripts_for(tmp_path / "projects", [pane])
    result = evaluate(home, FakeHerdrClient(panes=[pane], labels={"w1": "wh dev"}), NOW, root)
    assert isinstance(result, Evaluation)
    assert [p.pane_id for p in result.panes] == ["w4:p2"]
    assert isinstance(result.actions[0], Archive)
    assert result.labels == {"w1": "wh dev"}


def test_evaluate_does_not_persist_state(tmp_path):
    """The TUI polls every 2s. If evaluation wrote state, merely watching the
    dashboard would advance idle clocks and change what the timer does."""
    pane = mkpane("w4:p2")
    home = AtticHome(tmp_path)
    home.ensure()
    evaluate(home, FakeHerdrClient(panes=[pane]), NOW, tmp_path / "projects")
    assert home.load_state() == {}


def test_evaluate_applies_the_resumability_gate(tmp_path):
    """No transcript on disk, so the Archive is downgraded to a Skip."""
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    empty = tmp_path / "projects"
    empty.mkdir()
    result = evaluate(home, FakeHerdrClient(panes=[pane]), NOW, empty)
    assert isinstance(result.actions[0], Skip)
    assert "claude --resume would fail" in result.actions[0].reason


def test_gate_downgrades_archive_but_leaves_skips_alone(tmp_path):
    pane = mkpane("w4:p2")
    empty = tmp_path / "projects"
    empty.mkdir()
    original = Skip(pane, "pinned")
    assert gate_on_resumability([original], empty) == [original]


def test_run_tick_and_evaluate_produce_identical_verdicts(tmp_path):
    """The property this extraction exists to guarantee. If these ever diverge,
    the Fleet view is lying about what the reaper will do."""
    from attic.cli import run_tick
    pane = mkpane("w4:p2")
    root = transcripts_for(tmp_path / "projects", [pane])

    home_a = home_with_clock(tmp_path / "a", [pane])
    direct = evaluate(home_a, FakeHerdrClient(panes=[pane]), NOW, root)

    home_b = home_with_clock(tmp_path / "b", [pane])
    ticked = run_tick(home_b, FakeHerdrClient(panes=[pane]), NOW, dry_run=True,
                      projects_root=root)

    # Compare the whole verdict, not just its type: matching type names would
    # still pass if the two paths chose different panes, or skipped the same
    # pane for different reasons — which is exactly the drift this guards.
    def identity(actions):
        return [(type(a).__name__, a.pane.pane_id, getattr(a, "reason", None))
                for a in actions]

    assert identity(direct.actions) == identity(ticked.actions)


def test_the_tick_executes_under_the_config_it_decided_with(tmp_path):
    """One snapshot per tick. Re-reading the config after deciding lets a policy
    edit land mid-tick, so the daemon could act on a pane under a policy the
    user had just changed."""
    from attic.cli import run_tick

    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])

    reads = []
    real = home.load_config

    def counting_load_config():
        cfg = real()
        reads.append(cfg)
        return cfg

    home.load_config = counting_load_config
    run_tick(home, FakeHerdrClient(panes=[pane]), NOW, dry_run=True,
             projects_root=tmp_path / "projects")
    assert len(reads) == 1, f"config read {len(reads)}x in one tick; a policy edit between reads would split the tick"
