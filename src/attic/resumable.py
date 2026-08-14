"""Verifying that a session can actually be brought back before it is closed.

The tool's whole promise is that closed work returns. herdr reports a Claude
session UUID for every agent pane, and that UUID is genuine — but Claude Code
does not write the session's transcript immediately. A freshly started session
has a UUID that `claude --resume` cannot find:

    $ claude --resume 22222222-2222-4222-8222-222222222222
    No conversation found with session ID: 22222222-...

Archiving such a pane produces an archive that looks complete, restores
without error, and hands the user an empty prompt. This module is the gate
that prevents it: no pane is closed unless its transcript is already on disk.

Found by staging attic against a real herdr pane. Every unit test uses a fake
whose `pane_run` always succeeds, so nothing else in this project could catch
it — the defect lives in another program's persistence timing.
"""

from __future__ import annotations

from pathlib import Path

from .models import Pane

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def project_dir(cwd: str, root: Path | None = None) -> Path:
    """Claude Code's transcript directory for a working directory.

    The encoding — every "/" and "." becomes "-" — was verified against nine
    live sessions rather than inferred, including paths with dots
    (`/Users/x/.attic-stage/work` -> `-Users-x--attic-stage-work`).
    """
    return (root or CLAUDE_PROJECTS) / cwd.replace("/", "-").replace(".", "-")


def session_path(cwd: str, session_uuid: str, root: Path | None = None) -> Path:
    return project_dir(cwd, root) / f"{session_uuid}.jsonl"


def resume_blocker(pane: Pane, root: Path | None = None) -> str | None:
    """None if this pane's session is verifiably resumable, else the reason.

    Agents other than Claude are refused rather than trusted: this module only
    knows how to verify Claude's transcript layout, and closing a session whose
    recoverability we cannot check would be exactly the promise attic makes and
    cannot keep. Fails toward leaving the pane alive, like every other path here.
    """
    if pane.agent != "claude":
        return f"cannot verify resumability for agent {pane.agent!r}"
    if not pane.session_uuid:
        return "no session uuid"
    if not session_path(pane.cwd, pane.session_uuid, root).exists():
        return "session transcript not written yet; claude --resume would fail"
    return None
