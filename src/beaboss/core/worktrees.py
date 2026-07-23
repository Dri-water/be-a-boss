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


# --- delivery (landing a worker's branch) -------------------------------------
#
# Capability is a fact (is there a remote? is gh authed?), detected here; the
# *choice* of route is the orchestrator's. The local merge — the one irreversible
# step — is deterministic and gated (clean checkout, aborts on conflict, never
# force-anything); opening a PR is non-destructive.


async def current_branch(repo: Path) -> str | None:
    """The branch checked out in the repo's primary working copy (None if detached)."""
    code, out = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out if code == 0 and out and out != "HEAD" else None


async def has_remote(repo: Path) -> bool:
    code, out = await _git(repo, "remote")
    return code == 0 and bool(out.strip())


async def gh_available() -> bool:
    """True if the GitHub CLI is installed AND authenticated — i.e. PR mode is on."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "auth", "status",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await asyncio.wait_for(proc.wait(), timeout=15) == 0
    except (FileNotFoundError, OSError, asyncio.TimeoutError):
        return False


async def branch_diff(repo: Path, base: str, branch: str, max_chars: int = 3500) -> str:
    """A readable summary of what `branch` changed vs `base`.

    The file-level --stat is always kept (compact, and the most useful view of a
    large change); only the patch body is truncated. So a massive diff degrades to
    "here's every file that changed, plus a preview" rather than an unbounded dump
    that would blow past Telegram's message limit or flood the orchestrator.
    """
    _, stat = await _git(repo, "diff", "--stat", f"{base}...{branch}")
    _, patch = await _git(repo, "diff", f"{base}...{branch}")
    stat, patch = stat.strip(), patch.strip()
    if not stat and not patch:
        return "(no changes on the branch yet)"
    budget = max_chars - len(stat) - 4
    if budget < 200:  # the stat alone is already large — a very broad change
        return stat + "\n\n(patch omitted — many files changed; review via the PR or branch)"
    if len(patch) > budget:
        return (stat + "\n\n" + patch[:budget].rstrip()
                + "\n… (patch truncated — full diff on the branch/PR)")
    return stat + "\n\n" + patch


async def merge_into_current(repo: Path, branch: str) -> tuple[bool, str]:
    """Deterministically merge `branch` into the primary checkout's current branch.

    Requires the checkout to be clean; aborts and rolls back on conflict. Never
    force-pushes or discards anything — a refusal leaves the repo exactly as found.
    """
    base = await current_branch(repo)
    if base is None:
        return False, (f"{repo.name}'s main checkout is in a detached HEAD state — "
                       f"check out a branch there first")
    if not await is_clean(repo):
        return False, (f"{repo.name}'s working copy has uncommitted changes on {base} — "
                       f"commit or stash them first, then deliver again")
    code, out = await _git(
        repo, "merge", "--no-ff", "-m",
        f"Merge {branch} (delivered by be-a-boss)", branch)
    if code != 0:
        await _git(repo, "merge", "--abort")  # leave the checkout as we found it
        return False, (f"merging into {base} hit a conflict ({_tidy(out)}) — this one "
                       f"needs a human to resolve, or open a PR instead")
    return True, f"merged {branch} into {base}"


async def open_pr(repo: Path, branch: str) -> tuple[bool, str]:
    """Push `branch` and open a GitHub PR via gh. Non-destructive. Returns (ok, url|error)."""
    if not await has_remote(repo):
        return False, "no git remote is configured — add one, or deliver via a local merge"
    if not await gh_available():
        return False, ("the GitHub CLI isn't authenticated here — run `gh auth login` or "
                       "set GH_TOKEN, or deliver via a local merge")
    code, out = await _git(repo, "push", "-u", "origin", branch, timeout=120)
    if code != 0:
        return False, f"couldn't push {branch} to origin: {_tidy(out)}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "create", "--head", branch, "--fill", cwd=str(repo),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        pout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (FileNotFoundError, OSError):
        return False, "the GitHub CLI (gh) isn't installed here"
    except asyncio.TimeoutError:
        proc.kill()
        return False, "gh pr create timed out"
    text = pout.decode(errors="replace").strip()
    if (proc.returncode or 0) != 0:
        return False, f"gh pr create failed: {_tidy(text)}"
    return True, (text.splitlines()[-1] if text else "pull request opened")
