"""Overnight hours: idleness accrued while you are asleep must not count.

Without this, every session left open at bedtime is eligible the moment the
threshold passes overnight, so the first tick of the morning archives the work
you were in the middle of. The window both suppresses reaping AND re-stamps the
idle clock, so a pane idle since 21:00 starts its four hours over at 08:00
rather than arriving already eligible.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from test_policy import mkpane

from attic.policy import decide, in_quiet_hours, parse_quiet_hours, update_state
from attic.store import Config, PaneState

CHI = ZoneInfo("America/Chicago")


def at(y, m, d, hh, mm=0, tz=CHI):
    """A wall-clock local time, expressed as the UTC instant attic works in."""
    return datetime(y, m, d, hh, mm, tzinfo=tz).astimezone(UTC)


# --- parsing -----------------------------------------------------------------

def test_a_window_that_wraps_midnight_is_the_normal_case():
    start, end = parse_quiet_hours("22:00-08:00")
    assert (start.hour, end.hour) == (22, 8)


def test_a_window_inside_one_day_also_works():
    start, end = parse_quiet_hours("01:00-05:00")
    assert (start.hour, end.hour) == (1, 5)


@pytest.mark.parametrize("bad", ["", "22:00", "22:00-", "25:00-08:00",
                                 "22:00-08:00-09:00", "10pm-8am", "08:00-08:00"])
def test_malformed_windows_raise_rather_than_being_guessed_at(bad):
    """A misparsed window silently shifts when reaping happens by hours. Raising
    aborts the tick and archives nothing — the same direction iso() fails in."""
    with pytest.raises(ValueError):
        parse_quiet_hours(bad)


# --- window membership -------------------------------------------------------

def test_inside_a_wrapping_window_on_both_sides_of_midnight():
    assert in_quiet_hours(at(2026, 8, 14, 23, 30), "22:00-08:00", CHI)
    assert in_quiet_hours(at(2026, 8, 15, 2, 0), "22:00-08:00", CHI)


def test_outside_a_wrapping_window():
    assert not in_quiet_hours(at(2026, 8, 14, 12, 0), "22:00-08:00", CHI)
    assert not in_quiet_hours(at(2026, 8, 14, 21, 59), "22:00-08:00", CHI)


def test_the_boundaries_are_start_inclusive_and_end_exclusive():
    """08:00 must be OUT, or the first working minute of the day still counts as
    quiet and the clock keeps resetting after you sit down."""
    assert in_quiet_hours(at(2026, 8, 14, 22, 0), "22:00-08:00", CHI)
    assert not in_quiet_hours(at(2026, 8, 14, 8, 0), "22:00-08:00", CHI)
    assert in_quiet_hours(at(2026, 8, 14, 7, 59), "22:00-08:00", CHI)


def test_a_non_wrapping_window():
    assert in_quiet_hours(at(2026, 8, 14, 3, 0), "01:00-05:00", CHI)
    assert not in_quiet_hours(at(2026, 8, 14, 6, 0), "01:00-05:00", CHI)


def test_no_window_configured_means_never_quiet():
    assert not in_quiet_hours(at(2026, 8, 14, 3, 0), None, CHI)


def test_the_window_follows_local_time_across_a_dst_change():
    """The US spring-forward night: 02:00 local does not exist. Both instants are
    inside the window, and the window is still ten hours of wall clock."""
    assert in_quiet_hours(at(2026, 3, 8, 1, 30), "22:00-08:00", CHI)
    assert in_quiet_hours(at(2026, 3, 8, 3, 30), "22:00-08:00", CHI)
    assert not in_quiet_hours(at(2026, 3, 8, 9, 0), "22:00-08:00", CHI)


# --- the clock ---------------------------------------------------------------

def test_the_idle_clock_is_restamped_during_the_window():
    """This IS the reset: each quiet tick moves first_idle_at forward, so when
    the window ends the clock is only minutes old."""
    pane = mkpane("w1:p1")
    cfg = Config(quiet_hours="22:00-08:00")
    state = {pane.terminal_id: PaneState("2026-08-14T02:00:00Z", pane.revision)}

    late = at(2026, 8, 14, 23, 0)
    out = update_state([pane], state, late, cfg, tz=CHI)
    assert out[pane.terminal_id].first_idle_at == "2026-08-15T04:00:00Z"  # == 23:00 CHI


def test_the_idle_clock_is_left_alone_outside_the_window():
    pane = mkpane("w1:p1")
    cfg = Config(quiet_hours="22:00-08:00")
    stamp = "2026-08-14T14:00:00Z"
    state = {pane.terminal_id: PaneState(stamp, pane.revision)}

    out = update_state([pane], state, at(2026, 8, 14, 12, 0), cfg, tz=CHI)
    assert out[pane.terminal_id].first_idle_at == stamp


# --- the verdict -------------------------------------------------------------

def test_nothing_is_archived_during_the_window():
    pane = mkpane("w1:p1")
    cfg = Config(quiet_hours="22:00-08:00")
    state = {pane.terminal_id: PaneState("2026-08-14T02:00:00Z", pane.revision)}
    actions = decide([pane], state, at(2026, 8, 14, 23, 0), cfg, tz=CHI)
    assert type(actions[0]).__name__ == "Skip"
    assert "overnight hours" in actions[0].reason


def test_the_skip_reason_names_the_resolved_zone():
    """A misresolved timezone shifts the window by hours. Naming the zone in the
    reason makes that visible in `attic reap --dry-run` and the inventory,
    instead of silently changing when sessions get reaped."""
    pane = mkpane("w1:p1")
    cfg = Config(quiet_hours="22:00-08:00")
    actions = decide([pane], {}, at(2026, 8, 14, 23, 0), cfg, tz=CHI)
    assert "22:00-08:00" in actions[0].reason
    assert "Chicago" in actions[0].reason


# --- the behaviour the feature exists for ------------------------------------

def test_a_session_idle_overnight_is_not_archived_at_breakfast():
    """The whole point. Idle since 21:00, four-hour threshold: without quiet
    hours it is eligible by 01:00 and the 08:05 tick closes it."""
    pane = mkpane("w1:p1")
    cfg = Config(quiet_hours="22:00-08:00", idle_threshold_hours=4.0)
    state = {pane.terminal_id: PaneState(None, pane.revision)}

    # Ticks every five minutes from 21:00 through 12:05, as the daemon would.
    now = at(2026, 8, 14, 21, 0)
    archived_at = None
    for _ in range(190):
        state = update_state([pane], state, now, cfg, tz=CHI)
        actions = decide([pane], state, now, cfg, tz=CHI)
        if type(actions[0]).__name__ == "Archive":
            archived_at = now
            break
        now = now.replace(microsecond=0) + (at(2026, 1, 1, 0, 5) - at(2026, 1, 1, 0, 0))

    assert archived_at is not None, "never archived at all"
    local = archived_at.astimezone(CHI)
    # 11:55, not 12:00: the last tick inside the window is 07:55 (08:00 is
    # already the working day), so the clock resets there and the four hours
    # elapse one tick early. The guarantee is "a full threshold of waking time",
    # accurate to the tick interval — not "exactly threshold hours after 08:00".
    assert local.strftime("%H:%M") == "11:55", f"archived at {local:%H:%M} local"
    assert local.hour >= 11, "must not be archived during or right after the window"


def test_without_quiet_hours_the_same_session_is_archived_overnight():
    """Proves the test above is measuring quiet hours and not something else."""
    pane = mkpane("w1:p1")
    cfg = Config(idle_threshold_hours=4.0)  # no window
    state = {pane.terminal_id: PaneState(None, pane.revision)}

    now = at(2026, 8, 14, 21, 0)
    archived_at = None
    for _ in range(190):
        state = update_state([pane], state, now, cfg, tz=CHI)
        actions = decide([pane], state, now, cfg, tz=CHI)
        if type(actions[0]).__name__ == "Archive":
            archived_at = now
            break
        now = now + (at(2026, 1, 1, 0, 5) - at(2026, 1, 1, 0, 0))

    assert archived_at is not None
    assert archived_at.astimezone(CHI).hour < 8


# --- the pipeline actually uses it -------------------------------------------

def test_evaluate_applies_quiet_hours_end_to_end(tmp_path):
    """update_state's config argument was optional at first, so evaluate() did
    not pass it: reaping was suppressed but the clock was never reset, and all
    other tests still passed. This asserts the real pipeline, not the policy
    function in isolation.

    The window is computed from the current system-local time so the test is
    deterministic on any machine — evaluate() resolves the system zone itself.
    """
    import json

    from fakes import FakeHerdrClient

    from attic.evaluate import evaluate
    from attic.store import AtticHome

    now = datetime.now(UTC)
    local = now.astimezone()
    lo = (local.hour - 1) % 24
    hi = (local.hour + 1) % 24
    window = f"{lo:02d}:00-{hi:02d}:00"

    home = AtticHome(tmp_path)
    home.ensure()
    home.config_path.write_text(json.dumps({"quiet_hours": window}), encoding="utf-8")

    pane = mkpane("w1:p1")
    stale = "2020-01-01T00:00:00Z"          # idle for years; without the window this reaps
    home.state_path.write_text(json.dumps(
        {pane.terminal_id: {"first_idle_at": stale, "last_revision": pane.revision}}),
        encoding="utf-8")

    ev = evaluate(home, FakeHerdrClient(panes=[pane]), now, tmp_path / "projects")

    assert [type(a).__name__ for a in ev.actions] == ["Skip"]
    assert "overnight hours" in ev.actions[0].reason
    assert ev.state[pane.terminal_id].first_idle_at != stale, "clock was not reset"
