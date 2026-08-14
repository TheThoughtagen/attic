"""The gate that keeps attic's promise: never close what cannot come back.

These tests exist because a real staging run closed a real pane and then failed
to restore it — `claude --resume <uuid>` answered "No conversation found".
herdr's session UUID is genuine, but Claude Code writes the transcript lazily,
so a young session's UUID resolves to nothing.
"""

from pathlib import Path

from test_policy import mkpane

from attic.resumable import project_dir, resume_blocker, session_path


def make_transcript(root: Path, cwd: str, uuid: str) -> Path:
    path = session_path(cwd, uuid, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    return path


def test_encoding_matches_claude_code_layout(tmp_path):
    """Verified against nine live sessions: every "/" and "." becomes "-"."""
    assert project_dir("/Users/you", tmp_path).name == "-Users-you"
    assert project_dir("/Users/p/data/projects/analytics", tmp_path).name == (
        "-Users-p-data-projects-analytics"
    )
    # A dotted path is the case that breaks a naive "/"-only replacement.
    assert project_dir("/Users/p/.attic-stage/work", tmp_path).name == (
        "-Users-p--attic-stage-work"
    )


def test_pane_with_a_written_transcript_is_resumable(tmp_path):
    pane = mkpane("w1:p1")
    make_transcript(tmp_path, pane.cwd, pane.session_uuid)
    assert resume_blocker(pane, tmp_path) is None


def test_pane_whose_transcript_is_not_written_yet_is_blocked(tmp_path):
    """The exact defect found in staging: a real UUID, no transcript, and
    `claude --resume` fails after the pane has already been closed."""
    pane = mkpane("w1:p1")
    blocker = resume_blocker(pane, tmp_path)
    assert blocker is not None
    assert "claude --resume would fail" in blocker


def test_a_sibling_session_in_the_same_project_does_not_count(tmp_path):
    """Presence of *some* transcript in the directory is not enough — the check
    must be for this pane's own session id."""
    pane = mkpane("w1:p1")
    make_transcript(tmp_path, pane.cwd, "some-other-session-uuid")
    assert resume_blocker(pane, tmp_path) is not None


def test_non_claude_agents_are_refused_not_trusted(tmp_path):
    """We only know how to verify Claude's layout. Closing a session whose
    recoverability cannot be checked is the promise attic cannot keep."""
    pane = mkpane("w1:p1")
    pane = type(pane)(**{**pane.__dict__, "agent": "codex"})
    blocker = resume_blocker(pane, tmp_path)
    assert blocker is not None
    assert "codex" in blocker


def test_missing_session_uuid_is_blocked(tmp_path):
    pane = mkpane("w1:p1")
    pane = type(pane)(**{**pane.__dict__, "session_uuid": None})
    assert resume_blocker(pane, tmp_path) == "no session uuid"
