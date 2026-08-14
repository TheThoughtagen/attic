"""Parsing of herdr's `pane list` JSON into immutable Pane records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pane:
    pane_id: str
    terminal_id: str
    workspace_id: str
    tab_id: str
    agent: str | None
    agent_status: str
    session_uuid: str | None
    cwd: str
    title: str
    focused: bool
    revision: int
    scroll_rows: int

    @property
    def is_agent(self) -> bool:
        return self.agent is not None

    @classmethod
    def from_json(cls, obj: dict) -> "Pane":
        session = obj.get("agent_session") or {}
        scroll = obj.get("scroll") or {}
        return cls(
            pane_id=obj["pane_id"],
            terminal_id=obj["terminal_id"],
            workspace_id=obj.get("workspace_id", ""),
            tab_id=obj.get("tab_id", ""),
            agent=obj.get("agent"),
            agent_status=obj.get("agent_status", "unknown"),
            session_uuid=session.get("value"),
            cwd=obj.get("cwd", ""),
            title=obj.get("terminal_title_stripped") or obj.get("terminal_title", ""),
            focused=bool(obj.get("focused", False)),
            revision=int(obj.get("revision", 0)),
            scroll_rows=int(scroll.get("max_offset_from_bottom", 0))
            + int(scroll.get("viewport_rows", 0)),
        )


def parse_pane_list(payload: dict) -> list[Pane]:
    """Accept either the full CLI envelope or a bare {"panes": [...]} object.

    A pane missing pane_id or terminal_id is skipped, not fatal: it cannot be
    identified, so it can never be archived — and raising here would escape
    HerdrClient's HerdrError contract and kill the unattended tick.
    """
    node = payload.get("result", payload)
    return [
        Pane.from_json(p)
        for p in node.get("panes", [])
        if isinstance(p, dict) and p.get("pane_id") and p.get("terminal_id")
    ]
