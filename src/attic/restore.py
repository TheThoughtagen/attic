"""Bring an archived session back as a live herdr pane."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .archive import Archiver
from .policy import iso
from .store import AtticHome


def restore(home: AtticHome, client, manifest: dict, now: datetime) -> str:
    """Open a new tab in the focused workspace running the archived session.

    Non-destructive: the archive is kept and the index gains a restored_at entry.
    """
    cwd = manifest["cwd"]
    if not Path(cwd).is_dir():
        raise FileNotFoundError(f"cwd no longer exists: {cwd}")

    pane_id = client.tab_create(cwd, manifest.get("title", manifest["id"]))

    # Execute the argv recorded at archive time, not one rebuilt from today's code.
    # The tab is already opened in `cwd`, so the manifest's display string keeps its
    # redundant "cd ... &&" prefix for humans while this stays a clean token list.
    argv = manifest.get("resume_argv")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        # Manifest predates resume_argv, or the field is corrupt.
        argv = ["claude", "--resume", manifest["session_uuid"]]
    client.pane_run(pane_id, argv)

    Archiver(home, client).append_index({
        "id": manifest["id"],
        "restored_at": iso(now),
        "restored_into": pane_id,
    })
    return pane_id
