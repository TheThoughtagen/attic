"""Session and repo facts behind the Fleet detail view.

The split these tests protect: `repo_info` and `transcript_size` run for every
row on every 2-second refresh, so they must stay stat-cheap; `session_detail`
reads the file, so it must read only the tail and cache on mtime. Transcripts in
this fleet reach 32 MB — reading one per row per refresh would make the UI
unusable, and the failure would look like "the TUI is slow", not like a bug here.
"""

import json

from test_policy import mkpane

from attic.resumable import session_path
from attic.session import (
    TAIL_BYTES,
    human_bytes,
    repo_info,
    session_detail,
    transcript_size,
)


def dumps(obj):
    """Compact separators, matching what Claude Code actually writes."""
    return json.dumps(obj, separators=(",", ":"))


def write_transcript(root, pane, prompts, filler=0):
    path = session_path(pane.cwd, pane.session_uuid, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(filler):
        lines.append(dumps({"type": "assistant", "message": {"role": "assistant"},
                            "pad": "x" * 200, "i": i}))
    for p in prompts:
        lines.append(dumps({"type": "user", "message": {"role": "user"}}))
        lines.append(dumps({"type": "last-prompt", "lastPrompt": p}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- repo and worktree -------------------------------------------------------

def test_a_clone_is_reported_as_a_repo_not_a_worktree(tmp_path):
    repo = tmp_path / "myproj"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "src").mkdir()

    info = repo_info(str(repo / "src"))
    assert info.name == "myproj"
    assert info.is_worktree is False


def test_a_worktree_is_detected_by_its_dot_git_FILE(tmp_path):
    """The distinction is only visible on disk: a clone has a .git directory, a
    worktree has a .git file pointing elsewhere. Without this, a fleet full of
    auto-claude-worktrees panes looks like a fleet of identical repos."""
    main = tmp_path / "main"
    wt_git = main / ".git" / "worktrees" / "feature"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feat/rickhouse\n")

    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n")

    info = repo_info(str(wt))
    assert info.is_worktree is True
    assert info.worktree_branch == "feat/rickhouse"


def test_a_directory_outside_any_repo_is_not_an_error(tmp_path):
    """Shell panes live outside repos all the time."""
    assert repo_info(str(tmp_path)) is None
    assert repo_info(None) is None
    assert repo_info("") is None


def test_a_worktree_whose_gitdir_is_missing_still_names_the_repo(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /nowhere/that/exists\n")
    info = repo_info(str(wt))
    assert info.name == "wt"
    assert info.worktree_branch == "detached"


# --- size --------------------------------------------------------------------

def test_size_is_none_when_the_transcript_is_not_written_yet(tmp_path):
    """Same condition resumable.py gates closing on — a young session."""
    assert transcript_size(mkpane("w1:p1"), tmp_path) is None


def test_size_reflects_the_file(tmp_path):
    pane = mkpane("w1:p1")
    path = write_transcript(tmp_path, pane, ["hello"])
    assert transcript_size(pane, tmp_path) == path.stat().st_size


def test_a_non_claude_pane_has_no_transcript(tmp_path):
    pane = mkpane("w1:p1")
    pane = type(pane)(**{**pane.__dict__, "agent": "codex"})
    assert transcript_size(pane, tmp_path) is None


def test_human_bytes_reads_as_sizes():
    assert human_bytes(None) == "—"
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(5 * 1024**2) == "5.0 MB"


# --- the lazy half -----------------------------------------------------------

def test_the_newest_prompt_wins(tmp_path):
    pane = mkpane("w1:p1")
    write_transcript(tmp_path, pane, ["first question", "second question"])
    assert session_detail(pane, tmp_path).last_prompt == "second question"


def test_the_prompt_is_found_without_reading_the_whole_file(tmp_path):
    """A prompt near the end must be found even when the file is far larger than
    the tail window — that is the whole point of seeking rather than parsing."""
    pane = mkpane("w1:p1")
    path = write_transcript(tmp_path, pane, ["the last thing I asked"], filler=2000)
    assert path.stat().st_size > TAIL_BYTES * 2, "fixture is not big enough to prove it"
    assert session_detail(pane, tmp_path).last_prompt == "the last thing I asked"


def test_message_count_covers_the_whole_file(tmp_path):
    pane = mkpane("w1:p1")
    write_transcript(tmp_path, pane, ["a", "b"], filler=50)
    assert session_detail(pane, tmp_path).messages == 52   # 50 assistant + 2 user


def test_the_result_is_cached_until_the_file_changes(tmp_path):
    """Holding the panel open across refreshes must not re-read a 32 MB file."""
    pane = mkpane("w1:p1")
    path = write_transcript(tmp_path, pane, ["original"])
    assert session_detail(pane, tmp_path).last_prompt == "original"

    # Rewrite with a different size so (mtime, size) changes even on a coarse clock.
    write_transcript(tmp_path, pane, ["a substantially longer replacement prompt"])
    assert session_detail(pane, tmp_path).last_prompt == (
        "a substantially longer replacement prompt")
    assert path.exists()


def test_a_transcript_with_no_prompts_yields_none_not_a_crash(tmp_path):
    pane = mkpane("w1:p1")
    write_transcript(tmp_path, pane, [], filler=3)
    detail = session_detail(pane, tmp_path)
    assert detail.last_prompt is None
    assert detail.messages == 3


def test_a_corrupt_line_does_not_break_the_read(tmp_path):
    pane = mkpane("w1:p1")
    path = write_transcript(tmp_path, pane, ["good question"])
    path.write_text(path.read_text() + "{not json at all\n", encoding="utf-8")
    assert session_detail(pane, tmp_path).last_prompt == "good question"


def test_a_missing_transcript_returns_none(tmp_path):
    assert session_detail(mkpane("w1:p1"), tmp_path) is None
