from datetime import datetime, timedelta, timezone

import pytest

from attic.exempt import resolve_terminal_id, set_pinned, set_snooze
from attic.store import AtticHome, PaneState
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def test_resolves_a_pane_id_to_its_terminal_id():
    panes = [mkpane("w4:p2", terminal_id="term_abc")]
    assert resolve_terminal_id(panes, "w4:p2") == "term_abc"


def test_accepts_a_terminal_id_directly():
    panes = [mkpane("w4:p2", terminal_id="term_abc")]
    assert resolve_terminal_id(panes, "term_abc") == "term_abc"


def test_unknown_identifier_raises_lookup_error():
    with pytest.raises(LookupError, match="w9:p9"):
        resolve_terminal_id([mkpane("w4:p2")], "w9:p9")


def test_pinning_is_stored_under_the_terminal_id_not_the_pane_id(tmp_path):
    """Pane IDs are positional and recycled. A pin stored under 'w4:p2' would
    protect the slot, so a new session opening there inherits it silently."""
    home = AtticHome(tmp_path)
    home.ensure()
    panes = [mkpane("w4:p2", terminal_id="term_original")]
    set_pinned(home, resolve_terminal_id(panes, "w4:p2"), True)
    assert home.load_state()["term_original"].pinned is True
    assert "w4:p2" not in home.load_state()


def test_a_new_terminal_in_the_same_pane_slot_is_not_pinned(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    set_pinned(home, resolve_terminal_id([mkpane("w4:p2", terminal_id="term_old")], "w4:p2"), True)
    recycled = [mkpane("w4:p2", terminal_id="term_new")]
    state = home.load_state()
    assert state.get(resolve_terminal_id(recycled, "w4:p2")) is None


def test_pinning_preserves_an_existing_idle_clock(tmp_path):
    """Protection must not reset the clock — guards gate execution, not observation."""
    home = AtticHome(tmp_path)
    home.ensure()
    home.save_state({"term_abc": PaneState("2026-08-13T02:00:00Z", 7)})
    set_pinned(home, "term_abc", True)
    entry = home.load_state()["term_abc"]
    assert entry.first_idle_at == "2026-08-13T02:00:00Z"
    assert entry.last_revision == 7
    assert entry.pinned is True


def test_snooze_returns_the_previous_deadline_so_it_can_be_reported(tmp_path):
    """Re-snoozing replaces rather than stacks, which can shorten protection —
    so the caller must be able to say what it changed."""
    home = AtticHome(tmp_path)
    home.ensure()
    set_snooze(home, "term_abc", NOW + timedelta(hours=8))
    previous = set_snooze(home, "term_abc", NOW + timedelta(hours=1))
    assert previous == "2026-08-13T20:00:00Z"
    assert home.load_state()["term_abc"].snooze_until == "2026-08-13T13:00:00Z"


def test_unsnooze_clears_the_deadline(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    set_snooze(home, "term_abc", NOW + timedelta(hours=8))
    set_snooze(home, "term_abc", None)
    assert home.load_state()["term_abc"].snooze_until is None


def test_mutate_rejects_an_unknown_field(tmp_path):
    """setattr would accept a typo, create a phantom attribute, and let save_state
    drop it silently — a pin that reports success and never persists."""
    from attic.exempt import _mutate
    home = AtticHome(tmp_path)
    home.ensure()
    with pytest.raises(ValueError, match="snoozed_until"):
        _mutate(home, "term_abc", snoozed_until="2026-08-14T00:00:00Z")
