"""Facts about a pane's session and working directory, for the Fleet detail view.

Everything here touches the filesystem, so nothing on the tick path imports it —
`tests/test_daemon_purity.py` keeps that true. A slow, huge, or unreadable
transcript must never be able to delay or break reaping.

Two costs are deliberately kept apart:

  cheap   `transcript_size` and `repo_info` are stat-and-walk calls, computed for
          every row on every 2-second refresh.
  lazy    `last_prompt` and `message_count` read the file, so they are computed
          only for the selected row, only while the detail panel is open, and
          cached on (path, mtime, size).

Transcripts in this fleet reach 32 MB, which is why `last_prompt` reads the tail
rather than the file: Claude Code appends a `last-prompt` record after each turn,
so the newest one is always near the end.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import Pane
from .resumable import session_path

_MESSAGE_RE = re.compile(rb'"type"\s*:\s*"(?:user|assistant)"')
"""Counted by regex rather than json.loads: a 32 MB transcript is ~50k lines and
parsing every one costs seconds. Tolerant of spacing so a change in how Claude
Code serialises records cannot silently zero the count."""

TAIL_BYTES = 65_536
"""How much of the end of a transcript to read. Comfortably covers many turns of
`last-prompt` records while costing the same on a 32 MB file as on a 2 KB one."""


@dataclass(frozen=True)
class RepoInfo:
    name: str
    root: str
    worktree_branch: str | None = None
    """Set only when the directory is a git WORKTREE rather than a clone.

    The distinction is visible on disk: a clone has a `.git` directory, a
    worktree has a `.git` FILE containing `gitdir: ...`. It matters here because
    a fleet full of `auto-claude-worktrees` panes otherwise looks like a fleet of
    identical repos.
    """

    @property
    def is_worktree(self) -> bool:
        return self.worktree_branch is not None


def _branch_from_head(git_dir: Path) -> str | None:
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref: ") else head[:8]


def repo_info(cwd: str | None) -> RepoInfo | None:
    """Walk up from a working directory to the repo (or worktree) containing it.

    Returns None outside a repo, which is a normal state for a shell pane, not
    an error.
    """
    if not cwd:
        return None
    try:
        here = Path(cwd).resolve()
    except (OSError, ValueError):
        return None
    for d in (here, *here.parents):
        dot = d / ".git"
        if dot.is_dir():
            return RepoInfo(name=d.name, root=str(d))   # a clone: not a worktree
        if dot.is_file():
            try:
                text = dot.read_text(encoding="utf-8").strip()
            except OSError:
                return RepoInfo(name=d.name, root=str(d))
            gitdir = text.removeprefix("gitdir:").strip() if text.startswith("gitdir:") else ""
            branch = _branch_from_head(Path(gitdir)) if gitdir else None
            return RepoInfo(name=d.name, root=str(d), worktree_branch=branch or "detached")
    return None


def transcript_size(pane: Pane, root: Path | None = None) -> int | None:
    """Bytes of the session transcript on disk, or None if it isn't written yet.

    One stat call — cheap enough for every row on every refresh. A missing file
    is the normal state for a young session (see resumable.py), not a failure.
    """
    if pane.agent != "claude" or not pane.session_uuid or not pane.cwd:
        return None
    try:
        return session_path(pane.cwd, pane.session_uuid, root).stat().st_size
    except OSError:
        return None


def human_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    if n < 1024:
        return f"{n} B"
    for unit, div in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if n < div * 1024:
            return f"{n / div:.1f} {unit}"
    return f"{n / 1024**3:.1f} GB"


_cache: dict[str, tuple[tuple[int, int], SessionDetail]] = {}


@dataclass(frozen=True)
class SessionDetail:
    last_prompt: str | None
    messages: int


def session_detail(pane: Pane, root: Path | None = None) -> SessionDetail | None:
    """The expensive half: what you last asked, and how long the thread is.

    Cached on (mtime, size) so holding the panel open across refreshes costs one
    read per session, and a session that keeps working invalidates itself.
    """
    if pane.agent != "claude" or not pane.session_uuid or not pane.cwd:
        return None
    path = session_path(pane.cwd, pane.session_uuid, root)
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    stamp = (int(st.st_mtime), st.st_size)
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]

    prompt = None
    messages = 0
    try:
        with open(path, "rb") as fh:
            for raw in fh:                       # full pass: message count
                if _MESSAGE_RE.search(raw):
                    messages += 1
            fh.seek(max(0, st.st_size - TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in reversed(tail):                  # newest last-prompt wins
        if '"last-prompt"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "last-prompt" and rec.get("lastPrompt"):
            prompt = " ".join(str(rec["lastPrompt"]).split())
            break

    detail = SessionDetail(last_prompt=prompt, messages=messages)
    _cache[key] = (stamp, detail)
    return detail
