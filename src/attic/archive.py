"""Durable archives. Nothing here closes a pane; that is the caller's job,
and only when `archive()` returns a path."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from .herdr import HerdrError
from .policy import Archive, iso
from .store import AtticHome

SLUG_MAXLEN = 48


def slugify(title: str, maxlen: int = SLUG_MAXLEN) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug[:maxlen].rstrip("-")) or "untitled"


def make_archive_id(now: datetime, title: str, existing: set[str]) -> str:
    base = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{slugify(title)}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _write_fsynced(path: Path, text: str) -> None:
    # encoding is explicit, not locale-derived: scrollback is dense Unicode (box
    # drawing, spinners, emoji) and launchd starts jobs with no LANG set, so the
    # default encoding under the timer differs from the one in your shell. Guessing
    # wrong raises UnicodeEncodeError, and every archive silently fails forever.
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


class Archiver:
    def __init__(self, home: AtticHome, client) -> None:
        self.home = home
        self.client = client

    def archive(self, action: Archive, workspace_label: str, now: datetime) -> Path | None:
        """Write a durable archive. Return its path, or None if anything failed.

        A None return is a hard instruction to the caller: do not close this pane.
        """
        pane = action.pane
        try:
            scrollback = self.client.pane_read(pane.pane_id, max(pane.scroll_rows, 1))
        except HerdrError:
            return None
        if not scrollback.strip():
            return None

        self.home.ensure()
        existing = {p.name for p in self.home.archive_dir.iterdir() if p.is_dir()}
        archive_id = make_archive_id(now, pane.title, existing)
        path = self.home.archive_dir / archive_id

        manifest = {
            "id": archive_id,
            "pane_id": pane.pane_id,
            "terminal_id": pane.terminal_id,
            "workspace": workspace_label,
            "workspace_id": pane.workspace_id,
            "session_uuid": pane.session_uuid,
            "agent": pane.agent,
            "cwd": pane.cwd,
            "title": pane.title,
            "idle_since": iso(action.idle_since),
            "archived_at": iso(now),
            "scrollback_lines": len(scrollback.splitlines()),
            "resume": f"cd {pane.cwd} && claude --resume {pane.session_uuid}",
        }

        try:
            path.mkdir(parents=True)
            _write_fsynced(path / "scrollback.txt", scrollback)
            _write_fsynced(path / "manifest.json", json.dumps(manifest, indent=2))
            dir_fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            return None
        return path

    def append_index(self, entry: dict) -> None:
        self.home.ensure()
        with open(self.home.index_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
