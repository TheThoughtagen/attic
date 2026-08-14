"""Runs against the live herdr server. Excluded by default; run with:
    uv run pytest -m integration -v
"""

import shutil

import pytest

from attic.herdr import HerdrClient

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    if shutil.which("herdr") is None:
        pytest.skip("herdr not installed")
    return HerdrClient()


def test_protocol_matches_the_pinned_value(client):
    from attic.store import Config
    assert client.protocol() == Config().herdr_protocol, (
        "herdr protocol changed; review HerdrClient parsing, then bump "
        "Config.herdr_protocol only after re-verifying every command shape"
    )


def test_pane_list_shape_is_still_what_we_parse(client):
    panes = client.pane_list()
    assert panes, "expected at least one live pane"
    for p in panes:
        assert p.pane_id and p.terminal_id
        assert p.agent_status in {"idle", "working", "blocked", "done", "unknown"}, (
            f"unknown agent_status {p.agent_status!r} — herdr added a state; "
            "check whether policy.decide() should treat it as reapable"
        )


def test_agent_panes_expose_session_uuids(client):
    agents = [p for p in client.pane_list() if p.is_agent]
    if not agents:
        pytest.skip("no agent panes running")
    assert all(p.session_uuid for p in agents)


def test_pane_read_returns_output_for_a_live_pane(client):
    pane = client.pane_list()[0]
    text = client.pane_read(pane.pane_id, min(pane.scroll_rows or 50, 200))
    assert isinstance(text, str)


def test_workspace_labels_resolve(client):
    labels = client.workspace_labels()
    assert labels and all(isinstance(v, str) for v in labels.values())
