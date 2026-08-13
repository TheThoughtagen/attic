import json
from pathlib import Path

from attic.models import Pane, parse_pane_list

FIXTURE = Path(__file__).parent / "fixtures" / "pane_list_sample.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parses_every_pane_from_real_herdr_output():
    panes = parse_pane_list(load_fixture())
    assert len(panes) == 10


def test_agent_pane_carries_session_uuid():
    panes = {p.pane_id: p for p in parse_pane_list(load_fixture())}
    p = panes["w4:p2"]
    assert p.agent == "claude"
    assert p.session_uuid == "55555555-5555-4555-8555-555555555555"
    assert p.agent_status == "working"
    assert p.cwd == "/Users/you/data/projects/analytics"
    assert p.title == "◐ Debug batch transaction group logging in production"
    assert p.terminal_id == "term_658ed00535c1118"


def test_non_agent_pane_has_no_agent_or_session():
    panes = {p.pane_id: p for p in parse_pane_list(load_fixture())}
    p = panes["w3:p8"]          # an `nvim .` pane
    assert p.agent is None
    assert p.session_uuid is None
    assert p.agent_status == "unknown"


def test_scroll_rows_is_buffer_plus_viewport():
    panes = {p.pane_id: p for p in parse_pane_list(load_fixture())}
    p = panes["w3:p1"]          # max_offset_from_bottom 6314, viewport_rows 91
    assert p.scroll_rows == 6405


def test_pane_is_frozen():
    p = parse_pane_list(load_fixture())[0]
    try:
        p.revision = 1
    except Exception as exc:
        assert "frozen" in str(exc).lower() or isinstance(exc, AttributeError)
    else:
        raise AssertionError("Pane must be immutable")
