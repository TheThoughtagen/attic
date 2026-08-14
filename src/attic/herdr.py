"""The only module that talks to herdr. Everything else takes this as a dependency."""

from __future__ import annotations

import json
import subprocess
from typing import Callable

from .models import Pane, parse_pane_list


class HerdrError(Exception):
    """Any failure to obtain a usable answer from herdr."""


def _subprocess_runner(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise OSError(f"{' '.join(argv)} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


class HerdrClient:
    def __init__(self, runner: Callable[[list[str]], str] | None = None) -> None:
        self._run = runner or _subprocess_runner

    def _text(self, *args: str) -> str:
        try:
            return self._run(["herdr", *args])
        except Exception as exc:                      # OSError, timeout, missing binary
            raise HerdrError(f"herdr {' '.join(args)} failed: {exc}") from exc

    def _json(self, *args: str) -> dict:
        raw = self._text(*args)
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise HerdrError(f"herdr {' '.join(args)} returned non-JSON: {raw[:200]!r}") from exc
        # Valid JSON that is a list/string/number would sail through and raise
        # AttributeError at the first .get(). Callers catch HerdrError only.
        if not isinstance(parsed, dict):
            raise HerdrError(
                f"herdr {' '.join(args)} returned {type(parsed).__name__}, expected object"
            )
        return parsed

    def _result(self, *args: str) -> dict:
        """Fetch a command's `result` object, guaranteeing it is a dict."""
        node = self._json(*args).get("result", {})
        if not isinstance(node, dict):
            raise HerdrError(f"herdr {' '.join(args)}: result is not an object")
        return node

    def protocol(self) -> int:
        """Parse the protocol line from the `server:` block of `herdr status`.

        Both the `client:` and `server:` blocks carry a `protocol:` line; only
        the server's governs compatibility, so the client's line must be skipped.
        """
        text = self._text("status")
        in_server = False
        for line in text.splitlines():
            if line.startswith("server:"):
                in_server = True
                continue
            if line and not line[0].isspace():
                in_server = False
            if in_server and "protocol:" in line:
                value = line.split("protocol:")[1].strip()
                try:
                    return int(value)
                except ValueError as exc:
                    raise HerdrError(
                        f"herdr status reported non-integer protocol {value!r}"
                    ) from exc
        raise HerdrError("could not determine herdr server protocol")

    def pane_list(self) -> list[Pane]:
        # parse_pane_list accepts a bare {"panes": [...]} object as well as the envelope.
        return parse_pane_list(self._result("pane", "list"))

    def workspace_labels(self) -> dict[str, str]:
        # Malformed entries are skipped rather than fatal, consistent with
        # load_state: a missing label only costs a manifest the workspace's
        # display name, while raising would kill the whole unattended tick.
        node = self._result("workspace", "list")
        out: dict[str, str] = {}
        for w in node.get("workspaces", []):
            if isinstance(w, dict) and w.get("workspace_id"):
                out[w["workspace_id"]] = w.get("label", "")
        return out

    def pane_read(self, pane_id: str, lines: int) -> str:
        return self._text(
            "pane", "read", pane_id,
            "--source", "recent-unwrapped", "--lines", str(lines), "--format", "text",
        )

    def pane_close(self, pane_id: str) -> None:
        self._text("pane", "close", pane_id)

    def tab_create(self, cwd: str, label: str) -> str:
        """Return the pane_id of the new tab's root pane.

        Shape verified against live herdr 0.8.0 (protocol 19): the response is
        {"result": {"root_pane": {...,"pane_id": "w3:pC"}, "tab": {...}, "type":
        "tab_created"}}. Note `result.tab` carries NO pane list — the pane lives
        only under `result.root_pane`.
        """
        node = self._result("tab", "create", "--cwd", cwd, "--label", label, "--focus")
        root = node.get("root_pane")
        pane_id = root.get("pane_id") if isinstance(root, dict) else None
        if not pane_id:
            raise HerdrError("tab create returned no root pane")
        return pane_id

    def pane_run(self, pane_id: str, command: list[str]) -> None:
        self._text("pane", "run", pane_id, *command)
