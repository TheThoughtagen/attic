from datetime import UTC, datetime, timedelta, timezone

import pytest

from attic.models import Pane
from attic.policy import Archive, Skip, decide, iso, update_state
from attic.store import Config, PaneState

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
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
    """A closed pane's slot is reused: same pane_id, new terminal_id. If state were
    keyed by pane_id, the new pane would inherit the dead one's idle clock and be
    archived seconds after opening. The stale entry is deliberately keyed under the
    PANE id and given a matching revision, so a pane_id-keyed implementation would
    find it, judge the clock unchanged, and preserve the stale 02:00 timestamp."""
    prior = {"w4:p2": idle_since(10, revision=5)}
    pane = mkpane("w4:p2", terminal_id="term_new", revision=5)
    st = update_state([pane], prior, NOW)
    assert st["term_new"].first_idle_at == "2026-08-13T12:00:00Z"   # fresh, not 02:00
    assert "w4:p2" not in st


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
    # Selection is the safety property: the three longest-idle, not the first three seen.
    assert set(archived(actions)) == {"w1:p4", "w1:p1", "w1:p2"}   # 30h, 20h, 9h
    # With a 4h threshold all five are eligible, so BOTH shorter ones are capped.
    assert skip_reason(actions, "w1:p3") == "per-tick cap reached"   # 6h
    assert skip_reason(actions, "w1:p0") == "per-tick cap reached"   # 5h


def test_decide_returns_verdicts_in_input_order():
    """Output order is presentation, not policy: `attic reap --dry-run` prints one
    line per pane and must read in pane order for the operator's soak review."""
    panes, st = [], {}
    for i, hours in enumerate([5, 20, 9]):
        p = mkpane(f"w1:p{i}")
        panes.append(p)
        st[p.terminal_id] = idle_since(hours)
    actions = decide(panes, st, NOW, CFG)
    assert [a.pane.pane_id for a in actions] == ["w1:p0", "w1:p1", "w1:p2"]


def test_iso_normalizes_non_utc_input_to_z():
    """iso() enforces the UTC contract itself rather than trusting call sites."""
    mst = timezone(timedelta(hours=-6))
    assert iso(datetime(2026, 8, 13, 6, 0, 0, tzinfo=mst)) == "2026-08-13T12:00:00Z"


def test_iso_rejects_naive_datetime():
    """A naive datetime would be read as system local time, shifting the idle clock
    by the local UTC offset and archiving panes that are not eligible. Fail loudly."""
    with pytest.raises(ValueError, match="timezone-aware"):
        iso(datetime(2026, 8, 13, 12, 0, 0))  # noqa: DTZ001 — naive on purpose: that is what must raise


def test_every_pane_receives_a_verdict():
    panes = [mkpane("w1:p1"), mkpane("w1:p2", status="working"), mkpane("w1:p3", agent=None)]
    actions = decide(panes, {}, NOW, CFG)
    assert len(actions) == 3
    assert {a.pane.pane_id for a in actions} == {"w1:p1", "w1:p2", "w1:p3"}


def test_done_is_reapable_like_idle():
    """`done` is not a detection state — herdr's claude manifest defines only
    working/blocked/idle/unknown. It is a completion badge layered on top, and
    `herdr agent explain` reports such a pane as `idle` with a live process.
    Excluding it would ignore roughly half of a real machine's sessions."""
    st = {"term_w1:p1": idle_since(5)}
    assert archived(decide([mkpane(status="done")], st, NOW, CFG)) == ["w1:p1"]


def test_flipping_between_done_and_idle_does_not_reset_the_clock():
    """The badge is about the operator's attention, not the agent's activity, so
    a pane oscillating done<->idle must keep accruing idle time."""
    prior = {"term_w1:p1": idle_since(10, revision=5)}
    st = update_state([mkpane(status="done", revision=5)], prior, NOW)
    assert st["term_w1:p1"].first_idle_at == "2026-08-13T02:00:00Z"


def test_blocked_is_still_never_reapable_after_widening():
    """Widening to `done` must not accidentally admit `blocked`, which means the
    agent is parked on a permission prompt awaiting a human decision."""
    st = {"term_w1:p1": idle_since(1000)}
    actions = decide([mkpane(status="blocked")], st, NOW, CFG)
    assert archived(actions) == []


# --- exemptions -------------------------------------------------------------

def pinned_state(pinned=True, **kw):
    return PaneState(first_idle_at=(NOW - timedelta(hours=10)).isoformat().replace("+00:00", "Z"),
                     last_revision=1, pinned=pinned, **kw)


def test_pinned_pane_is_never_archived():
    st = {"term_w1:p1": pinned_state()}
    actions = decide([mkpane()], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "pinned"


def test_snoozed_pane_is_not_archived_before_the_deadline():
    st = {"term_w1:p1": pinned_state(pinned=False,
                                     snooze_until="2026-08-13T18:00:00Z")}   # NOW is 12:00
    actions = decide([mkpane()], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "snoozed until 2026-08-13T18:00:00Z"


def test_an_expired_snooze_stops_protecting():
    st = {"term_w1:p1": pinned_state(pinned=False,
                                     snooze_until="2026-08-13T06:00:00Z")}   # in the past
    assert archived(decide([mkpane()], st, NOW, CFG)) == ["w1:p1"]


def test_pin_outranks_status_in_the_reported_reason():
    """The operator's explicit intent is the more useful thing to show."""
    st = {"term_w1:p1": pinned_state()}
    actions = decide([mkpane(status="working")], st, NOW, CFG)
    assert skip_reason(actions, "w1:p1") == "pinned"


def test_exemptions_survive_update_state():
    prior = {"term_w1:p1": PaneState("2026-08-13T02:00:00Z", 5,
                                     snooze_until="2026-08-14T00:00:00Z", pinned=True)}
    st = update_state([mkpane(revision=5)], prior, NOW)
    assert st["term_w1:p1"].pinned is True
    assert st["term_w1:p1"].snooze_until == "2026-08-14T00:00:00Z"


def test_update_state_clears_an_expired_snooze():
    """So state.json does not accumulate stale deadlines forever."""
    prior = {"term_w1:p1": PaneState("2026-08-13T02:00:00Z", 5,
                                     snooze_until="2026-08-13T06:00:00Z")}
    st = update_state([mkpane(revision=5)], prior, NOW)
    assert st["term_w1:p1"].snooze_until is None
