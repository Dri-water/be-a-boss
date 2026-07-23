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


def _commit_in_worktree(dest: Path, filename: str) -> None:
    (dest / filename).write_text("x = 1\n")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-m", f"add {filename}")


def test_current_branch_and_no_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    assert asyncio.run(worktrees.current_branch(repo))  # some real branch name
    assert asyncio.run(worktrees.has_remote(repo)) is False


def test_branch_diff_shows_worker_changes(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    dest = asyncio.run(worktrees.create_worktree(repo, tmp_path / "wts", "nova"))
    _commit_in_worktree(dest, "feature.py")
    base = asyncio.run(worktrees.current_branch(repo))
    diff = asyncio.run(worktrees.branch_diff(repo, base, "worker/nova"))
    assert "feature.py" in diff


def test_merge_into_base_lands_the_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    dest = asyncio.run(worktrees.create_worktree(repo, tmp_path / "wts", "nova"))
    _commit_in_worktree(dest, "feature.py")
    base = asyncio.run(worktrees.current_branch(repo))
    landed, detail = asyncio.run(worktrees.merge_into_base(repo, "worker/nova", base))
    assert landed is True
    assert "merged" in detail
    assert (repo / "feature.py").exists()  # the work is now in the primary checkout


def test_merge_refuses_dirty_checkout(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    dest = asyncio.run(worktrees.create_worktree(repo, tmp_path / "wts", "nova"))
    _commit_in_worktree(dest, "feature.py")
    (repo / "dirty.txt").write_text("uncommitted\n")  # primary checkout not clean
    base = asyncio.run(worktrees.current_branch(repo))
    landed, detail = asyncio.run(worktrees.merge_into_base(repo, "worker/nova", base))
    assert landed is False
    assert "uncommitted" in detail
    assert not (repo / "feature.py").exists()  # nothing landed


def test_merge_refuses_wrong_base_branch(tmp_path):
    """The F1 fix: never merge into a branch other than the worker's fork point,
    even if the primary checkout has since moved to a different branch."""
    repo = _init_repo(tmp_path / "repo")
    dest = asyncio.run(worktrees.create_worktree(repo, tmp_path / "wts", "nova"))
    _commit_in_worktree(dest, "feature.py")
    fork = asyncio.run(worktrees.current_branch(repo))
    _git(repo, "checkout", "-b", "release")  # user switched the checkout
    landed, detail = asyncio.run(worktrees.merge_into_base(repo, "worker/nova", fork))
    assert landed is False
    assert "wrong branch" in detail or "forked from" in detail
    assert not (repo / "feature.py").exists()  # nothing landed on 'release'


def test_branch_ahead(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    dest = asyncio.run(worktrees.create_worktree(repo, tmp_path / "wts", "nova"))
    base = asyncio.run(worktrees.current_branch(repo))
    assert asyncio.run(worktrees.branch_ahead(repo, base, "worker/nova")) is False
    _commit_in_worktree(dest, "feature.py")
    assert asyncio.run(worktrees.branch_ahead(repo, base, "worker/nova")) is True


def test_branch_diff_degrades_gracefully_on_huge_change(tmp_path):
    """A massive diff keeps the file-level stat and truncates only the patch, so it
    stays well under Telegram's message limit instead of being an unbounded dump."""
    repo = _init_repo(tmp_path / "repo")
    dest = asyncio.run(worktrees.create_worktree(repo, tmp_path / "wts", "big"))
    (dest / "big.py").write_text("\n".join(f"line_{i} = {i}" for i in range(5000)) + "\n")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-m", "a huge change")
    base = asyncio.run(worktrees.current_branch(repo))
    diff = asyncio.run(worktrees.branch_diff(repo, base, "worker/big"))
    assert "big.py" in diff                              # the file summary survives
    assert "truncated" in diff or "omitted" in diff      # patch degraded, not dumped
    assert len(diff) < 4096                              # bounded under the hard limit
