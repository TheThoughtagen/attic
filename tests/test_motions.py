import pytest

from attic.tui.motions import MotionState


def test_single_g_is_pending_not_an_action():
    m = MotionState()
    assert m.feed("g") is None


def test_gg_goes_to_top():
    m = MotionState()
    m.feed("g")
    assert m.feed("g") == "top"


def test_gt_and_capital_gt_switch_tabs():
    m = MotionState()
    m.feed("g")
    assert m.feed("t") == "next_tab"
    m.feed("g")
    assert m.feed("T") == "prev_tab"


def test_count_prefixed_gt_jumps_to_a_tab():
    m = MotionState()
    assert m.feed("2") is None
    assert m.feed("g") is None
    assert m.feed("t") == "tab_2"


def test_an_unknown_key_after_g_clears_the_sequence():
    """Otherwise a stray key leaves the state machine armed and the NEXT
    keystroke does something the user did not ask for."""
    m = MotionState()
    m.feed("g")
    assert m.feed("x") is None
    assert m.feed("g") is None      # fresh sequence, still pending
    assert m.feed("g") == "top"


def test_shift_g_goes_to_bottom_without_a_prefix():
    assert MotionState().feed("G") == "bottom"


def test_pending_state_is_reported_for_the_status_line():
    m = MotionState()
    assert m.pending == ""
    m.feed("2")
    m.feed("g")
    assert m.pending == "2g"
    m.feed("t")
    assert m.pending == ""
