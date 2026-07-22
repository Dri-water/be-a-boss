import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from beaboss.core import worktrees


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _init_repo(path: Path) -> Path:
    """A git repo with one commit — a valid HEAD for worktree operations."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hi\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


def test_not_a_git_repo_is_friendly(tmp_path):
    """create_worktree on a plain dir explains the problem and the fix."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(worktrees.WorktreeError) as exc:
        asyncio.run(worktrees.create_worktree(plain, tmp_path / "wts", "nova"))
    msg = str(exc.value)
    assert "not a git repository" in msg
    assert "git init" in msg  # tells the user how to resolve it


def test_worktree_add_failure_is_digestible(tmp_path):
    """When git worktree add fails, the error names the branch and the fix,
    not a raw multi-line git dump."""
    repo = _init_repo(tmp_path / "repo")
    wts = tmp_path / "wts"
    # First worktree succeeds and checks out branch worker/nova.
    dest = asyncio.run(worktrees.create_worktree(repo, wts, "nova"))
    # Delete the directory without `git worktree remove`: git still considers
    # branch worker/nova checked out, so both add attempts now fail.
    shutil.rmtree(dest)
    with pytest.raises(worktrees.WorktreeError) as exc:
        asyncio.run(worktrees.create_worktree(repo, wts, "nova"))
    msg = str(exc.value)
    assert "worker/nova" in msg  # names the offending branch
    assert "retry" in msg       # points at a next step
    assert "\n" not in msg      # single, digestible line


def test_dirty_worktree_is_preserved_not_removed(tmp_path):
    """A dirty worktree is left in place with an explanation of how to proceed."""
    repo = _init_repo(tmp_path / "repo")
    wts = tmp_path / "wts"
    dest = asyncio.run(worktrees.create_worktree(repo, wts, "kite"))
    (dest / "scratch.txt").write_text("uncommitted work\n")  # make it dirty

    removed, detail = asyncio.run(worktrees.remove_worktree(repo, dest))

    assert removed is False
    assert dest.exists()  # work is not lost
    assert "uncommitted changes" in detail
    assert "stash" in detail or "--force" in detail  # how to proceed


def test_clean_worktree_is_removed(tmp_path):
    """Success path preserved: a clean worktree is removed."""
    repo = _init_repo(tmp_path / "repo")
    wts = tmp_path / "wts"
    dest = asyncio.run(worktrees.create_worktree(repo, wts, "ada"))

    removed, detail = asyncio.run(worktrees.remove_worktree(repo, dest))

    assert removed is True
    assert not dest.exists()


def test_is_git_repo(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert asyncio.run(worktrees.is_git_repo(repo)) is True
    assert asyncio.run(worktrees.is_git_repo(plain)) is False
