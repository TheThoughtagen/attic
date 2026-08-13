import json

import pytest

from attic.herdr import HerdrClient, HerdrError


class Recorder:
    """Stands in for subprocess: records argv, returns canned stdout."""

    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_protocol_parses_server_block():
    run = Recorder(["client:\n  protocol: 18\n\nserver:\n  status: running\n  protocol: 19\n"])
    assert HerdrClient(runner=run).protocol() == 19


def test_pane_list_returns_parsed_panes():
    payload = {"result": {"panes": [
        {"pane_id": "w1:p1", "terminal_id": "t1", "agent": "claude",
         "agent_status": "idle", "agent_session": {"value": "u-1"},
         "cwd": "/tmp", "terminal_title_stripped": "x", "revision": 3,
         "scroll": {"max_offset_from_bottom": 10, "viewport_rows": 5}}]}}
    client = HerdrClient(runner=Recorder([json.dumps(payload)]))
    panes = client.pane_list()
    assert [p.pane_id for p in panes] == ["w1:p1"]
    assert panes[0].scroll_rows == 15


def test_pane_read_sizes_the_request_and_returns_text():
    run = Recorder(["hello scrollback"])
    assert HerdrClient(runner=run).pane_read("w1:p1", 250) == "hello scrollback"
    assert run.calls[0] == [
        "herdr", "pane", "read", "w1:p1",
        "--source", "recent-unwrapped", "--lines", "250", "--format", "text",
    ]


def test_workspace_labels_maps_id_to_label():
    payload = {"result": {"workspaces": [
        {"workspace_id": "w3", "label": "clients"},
        {"workspace_id": "w4", "label": "wh dev"}]}}
    labels = HerdrClient(runner=Recorder([json.dumps(payload)])).workspace_labels()
    assert labels == {"w3": "clients", "w4": "wh dev"}


def test_malformed_json_raises_herdr_error():
    with pytest.raises(HerdrError):
        HerdrClient(runner=Recorder(["{not json"])).pane_list()


def test_subprocess_failure_raises_herdr_error():
    with pytest.raises(HerdrError):
        HerdrClient(runner=Recorder([OSError("socket gone")])).pane_list()


def test_tab_create_returns_root_pane_id():
    """Payload copied from a real `herdr tab create` response (protocol 19).
    The pane lives under result.root_pane; result.tab carries no pane list."""
    payload = {"id": "cli:tab:create", "result": {
        "root_pane": {"pane_id": "w4:p9", "tab_id": "w4:t9", "workspace_id": "w4",
                      "terminal_id": "term_abc", "cwd": "/private/tmp",
                      "agent_status": "unknown", "revision": 0},
        "tab": {"tab_id": "w4:t9", "label": "Some task", "workspace_id": "w4",
                "pane_count": 1},
        "type": "tab_created"}}
    client = HerdrClient(runner=Recorder([json.dumps(payload)]))
    assert client.tab_create("/tmp/repo", "Some task") == "w4:p9"


def test_tab_create_raises_when_no_root_pane():
    payload = {"result": {"tab": {"tab_id": "w4:t9"}, "type": "tab_created"}}
    client = HerdrClient(runner=Recorder([json.dumps(payload)]))
    with pytest.raises(HerdrError):
        client.tab_create("/tmp/repo", "Some task")
