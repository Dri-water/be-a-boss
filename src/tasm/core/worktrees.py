"""Git worktree isolation for coders. Every subprocess call is timeout-bounded.

A coder never works in the repo's primary checkout: it gets a linked worktree on
its own branch (coder/<id>). Teardown removes clean worktrees; dirty ones are
left in place and reported (firstmate practice: never delete un-merged work).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("tasm.core.worktrees")

GIT_TIMEOUT = 60  # seconds per git command


class WorktreeError(RuntimeError):
    pass


async def _git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
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


async def create_worktree(repo: Path, worktrees_dir: Path, coder_id: str) -> Path:
    """Create <worktrees_dir>/<coder_id> on new branch coder/<coder_id>."""
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    dest = worktrees_dir / coder_id
    if dest.exists():
        raise WorktreeError(f"worktree path already exists: {dest}")
    branch = f"coder/{coder_id}"
    code, out = await _git(repo, "worktree", "add", "-b", branch, str(dest))
    if code != 0:
        # branch may linger from an earlier run — retry without -b
        code2, out2 = await _git(repo, "worktree", "add", str(dest), branch)
        if code2 != 0:
            raise WorktreeError(f"worktree add failed:\n{out}\n{out2}")
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
        return False, f"worktree has uncommitted changes, left in place: {worktree}"
    code, out = await _git(repo, "worktree", "remove", str(worktree))
    if code != 0:
        return False, f"could not remove worktree: {out}"
    return True, "removed"
