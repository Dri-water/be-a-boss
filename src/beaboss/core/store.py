"""Restart-proof state: thread registry + fleet records. One JSON file, atomic
rewrite on change (small data, single event loop — same approach as before).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ports import Role


@dataclass
class ThreadRecord:
    """One thread the core knows about."""

    role: Role
    name: str
    cwd: str = ""            # repo (direct) or worktree (worker); "" = none yet
    session_id: str | None = None
    created_at: float = 0.0
    # worker-only:
    worker_id: str = ""       # short id, e.g. "nova"
    repo: str = ""           # the primary checkout the worktree came from
    task: str = ""           # the brief, verbatim
    worker_status: str = ""   # working | done | blocked | dismissed


class CoreStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "core.json"
        self._threads: dict[str, ThreadRecord] = {}
        self.orchestrator_thread: str | None = None
        self._load()

    # ---- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.orchestrator_thread = raw.get("orchestrator_thread")
        for k, v in raw.get("threads", {}).items():
            self._threads[k] = ThreadRecord(
                role=v.get("role", "direct"),
                name=v.get("name", ""),
                cwd=v.get("cwd", ""),
                session_id=v.get("session_id"),
                created_at=v.get("created_at", 0.0),
                worker_id=v.get("worker_id", ""),
                repo=v.get("repo", ""),
                task=v.get("task", ""),
                worker_status=v.get("worker_status", ""),
            )

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        payload = {
            "orchestrator_thread": self.orchestrator_thread,
            "threads": {k: asdict(v) for k, v in self._threads.items()},
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- API -------------------------------------------------------------

    def get(self, thread_id: str) -> ThreadRecord | None:
        return self._threads.get(thread_id)

    def all(self) -> dict[str, ThreadRecord]:
        return dict(self._threads)

    def put(self, thread_id: str, rec: ThreadRecord) -> None:
        if not rec.created_at:
            rec.created_at = time.time()
        self._threads[thread_id] = rec
        self._flush()

    def update(self, thread_id: str, **fields) -> None:
        rec = self._threads.get(thread_id)
        if rec is None:
            return
        changed = False
        for k, v in fields.items():
            if getattr(rec, k, None) != v:
                setattr(rec, k, v)
                changed = True
        if changed:
            self._flush()

    def delete(self, thread_id: str) -> None:
        if self._threads.pop(thread_id, None) is not None:
            self._flush()

    def set_orchestrator_thread(self, thread_id: str | None) -> None:
        if self.orchestrator_thread != thread_id:
            self.orchestrator_thread = thread_id
            self._flush()

    def workers(self) -> dict[str, ThreadRecord]:
        return {k: v for k, v in self._threads.items() if v.role == "worker"}
