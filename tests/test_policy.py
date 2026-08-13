from datetime import datetime, timedelta, timezone

from attic.models import Pane
from attic.policy import Archive, Skip, decide, update_state
from attic.store import Config, PaneState

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
CFG = Config()


def mkpane(pane_id="w1:p1", *, status="idle", agent="claude", uuid="u-1",
           focused=False, revision=1, terminal_id=None) -> Pane:
    return Pane(
        pane_id=pane_id,
        terminal_id=terminal_id or f"term_{pane_id}",
        workspace_id="w1", tab_id="w1:t1",
        agent=agent, agent_status=status, session_uuid=uuid,
        cwd="/tmp/repo", title="Some task", focused=focused,
        revision=revision, scroll_rows=100,
    )


def idle_since(hours: float, revision: int = 1) -> PaneState:
    return PaneState(
        first_idle_at=(NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
        last_revision=revision,
    )


def archived(actions) -> list[str]:
    return [a.pane.pane_id for a in actions if isinstance(a, Archive)]


def skip_reason(actions, pane_id: str) -> str:
    return next(a.reason for a in actions
                if isinstance(a, Skip) and a.pane.pane_id == pane_id)


# --- update_state ---------------------------------------------------------

def test_idle_pane_starts_the_clock():
    st = update_state([mkpane()], {}, NOW)
    assert st["term_w1:p1"].first_idle_at == "2026-08-13T12:00:00Z"


def test_revision_change_resets_the_clock():
    prior = {"term_w1:p1": idle_since(10, revision=5)}
    st = update_state([mkpane(revision=6)], prior, NOW)
    assert st["term_w1:p1"].first_idle_at == "2026-08-13T12:00:00Z"
    assert st["term_w1:p1"].last_revision == 6


def test_stable_revision_preserves_the_clock():
    prior = {"term_w1:p1": idle_since(10, revision=5)}
    st = update_state([mkpane(revision=5)], prior, NOW)
    assert st["term_w1:p1"].first_idle_at == "2026-08-13T02:00:00Z"


def test_non_idle_status_clears_the_clock():
    prior = {"term_w1:p1": idle_since(10)}
    st = update_state([mkpane(status="working")], prior, NOW)
    assert st["term_w1:p1"].first_idle_at is None


def test_vanished_panes_are_dropped_from_state():
    prior = {"term_gone": idle_since(10)}
    assert "term_gone" not in update_state([mkpane()], prior, NOW)


def test_recycled_pane_id_gets_a_fresh_clock():
    # Same pane_id, different terminal_id => must not inherit the old clock.
    prior = {"term_old": idle_since(10, revision=5)}
    st = update_state([mkpane("w4:p2", terminal_id="term_new")], prior, NOW)
    assert st["term_new"].first_idle_at == "2026-08-13T12:00:00Z"
    assert "term_old" not in st


# --- decide ---------------------------------------------------------------

def test_idle_past_threshold_is_archived():
    st = {"term_w1:p1": idle_since(5)}
    assert archived(decide([mkpane()], st, NOW, CFG)) == ["w1:p1"]


def test_idle_under_threshold_is_skipped():
    st = {"term_w1:p1": idle_since(3)}
    actions = decide([mkpane()], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "not idle long enough"


def test_working_pane_is_never_archived():
    st = {"term_w1:p1": idle_since(10)}
    actions = decide([mkpane(status="working")], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "status is working"


def test_blocked_pane_is_never_archived_at_any_age():
    st = {"term_w1:p1": idle_since(1000)}
    actions = decide([mkpane(status="blocked")], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "status is blocked"


def test_focused_pane_is_never_archived():
    st = {"term_w1:p1": idle_since(10)}
    actions = decide([mkpane(focused=True)], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "focused"


def test_pane_without_session_uuid_is_never_archived():
    st = {"term_w1:p1": idle_since(10)}
    actions = decide([mkpane(uuid=None)], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "no session uuid"


def test_non_agent_pane_is_never_archived():
    st = {"term_w1:p1": idle_since(10)}
    actions = decide([mkpane(agent=None, uuid=None, status="unknown")], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "not an agent pane"


def test_pane_with_no_recorded_clock_is_skipped():
    actions = decide([mkpane()], {}, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "idle clock not started"


def test_per_tick_cap_archives_longest_idle_first():
    panes, st = [], {}
    for i, hours in enumerate([5, 20, 9, 6, 30]):
        p = mkpane(f"w1:p{i}")
        panes.append(p)
        st[p.terminal_id] = idle_since(hours)
    actions = decide(panes, st, NOW, CFG)
    assert archived(actions) == ["w1:p4", "w1:p1", "w1:p2"]   # 30h, 20h, 9h
    assert skip_reason(actions, "w1:p3") == "per-tick cap reached"


def test_every_pane_receives_a_verdict():
    panes = [mkpane("w1:p1"), mkpane("w1:p2", status="working"), mkpane("w1:p3", agent=None)]
    actions = decide(panes, {}, NOW, CFG)
    assert len(actions) == 3
    assert {a.pane.pane_id for a in actions} == {"w1:p1", "w1:p2", "w1:p3"}
