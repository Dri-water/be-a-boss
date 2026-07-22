"""Git worktree isolation for workers. Every subprocess call is timeout-bounded.

A worker never works in the repo's primary checkout: it gets a linked worktree on
its own branch (worker/<id>). Teardown removes clean worktrees; dirty ones are
left in place and reported (never delete un-merged work).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("beaboss.core.worktrees")

GIT_TIMEOUT = 60  # seconds per git command


class WorktreeError(RuntimeError):
    pass


def _tidy(text: str) -> str:
    """Collapse git's multi-line output into one readable line for messages."""
    line = " ".join(text.split())
    return (line[:300] + "…") if len(line) > 300 else line


async def _git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as e:
        raise WorktreeError(
            "git is not installed or not on PATH — install git to use workers"
        ) from e
    except OSError as e:
        raise WorktreeError(f"could not run git in {cwd}: {e}") from e
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise WorktreeError(f"git {' '.join(args)} timed out after {timeout}s")
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    code, _ = await _git(path, "rev-parse", "--is-inside-work-tree")
    return code == 0


async def create_worktree(repo: Path, worktrees_dir: Path, worker_id: str) -> Path:
    """Create <worktrees_dir>/<worker_id> on new branch worker/<worker_id>."""
    if not await is_git_repo(repo):
        raise WorktreeError(
            f"not a git repository: {repo} — run `git init` there (and make an "
            f"initial commit), or point the worker at a repo under version control"
        )
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    dest = worktrees_dir / worker_id
    if dest.exists():
        raise WorktreeError(
            f"worktree path already exists: {dest} — an earlier worker may not "
            f"have been cleaned up; remove it (git worktree remove {dest}) and retry"
        )
    branch = f"worker/{worker_id}"
    code, out = await _git(repo, "worktree", "add", "-b", branch, str(dest))
    if code != 0:
        # branch may linger from an earlier run — retry attaching to it
        code2, out2 = await _git(repo, "worktree", "add", str(dest), branch)
        if code2 != 0:
            raise WorktreeError(
                f"could not create an isolated worktree for '{worker_id}'. "
                f"git said: {_tidy(out2 or out)}. Likely the branch '{branch}' "
                f"or path is already in use — remove the stale worktree "
                f"(git worktree remove) or branch (git branch -D {branch}) and retry"
            )
    log.info("worktree created repo=%s dest=%s branch=%s", repo, dest, branch)
    return dest


async def is_clean(worktree: Path) -> bool:
    code, out = await _git(worktree, "status", "--porcelain")
    return code == 0 and not out


async def remove_worktree(repo: Path, worktree: Path) -> tuple[bool, str]:
    """Remove if clean. Returns (removed, detail). Dirty → left in place."""
    if not worktree.exists():
        return True, "already gone"
    if not await is_clean(worktree):
        return False, (
            f"worktree has uncommitted changes, left in place: {worktree} — "
            f"commit or stash the work there first, then dismiss again; or to "
            f"discard it, force removal with `git worktree remove --force {worktree}`"
        )
    code, out = await _git(repo, "worktree", "remove", str(worktree))
    if code != 0:
        return False, (
            f"could not remove worktree {worktree}. git said: {_tidy(out)}. "
            f"You may need to remove it manually (git worktree remove {worktree})"
        )
    return True, "removed"
