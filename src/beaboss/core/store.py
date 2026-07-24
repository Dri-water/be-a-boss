"""Restart-proof state: thread registry + fleet records. One JSON file, atomic
rewrite on change (small data, single event loop — same approach as before).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from pathlib import Path

from .ports import Role

log = logging.getLogger("beaboss.core.store")

# Bump when the on-disk shape changes incompatibly; guards a self-developed change
# from silently mangling state written by a different version of the code.
# (office_message_ids was added as an additive field — old code ignores it, new code
# defaults it — so no bump was needed.)
SCHEMA_VERSION = 1

# Per-office cap on tracked message ids: a factory reset deletes these, and this keeps
# the state file bounded on a long-lived, chatty deployment. ~10k ids ≈ tens of KB.
OFFICE_MSG_CAP = 10_000


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
    base_branch: str = ""    # the branch the worker forked from (merge/PR target)
    checks: str = ""         # last run_checks verdict: "" | pass | fail
    checks_sha: str = ""     # branch tip when checks last ran (to detect staleness)
    task: str = ""           # the brief, verbatim
    worker_status: str = ""   # working | done | blocked | dismissed | delivered


class CoreStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "core.json"
        self._threads: dict[str, ThreadRecord] = {}
        self.orchestrator_thread: str | None = None
        self.dashboard_msg_id: int | None = None   # the pinned #general status board
        self.pending_delivery: dict[str, str] = {}  # worker_id -> method, awaiting /approve
        # Message ids in the orchestrator's offices (#general + DMs), keyed by chat id.
        # Worker topics are deleted wholesale on reset; these have no topic to drop, so
        # a factory reset deletes them by id. Bounded so it can't grow without limit.
        self.office_message_ids: dict[str, list[int]] = {}
        self._load()

    # ---- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # Don't silently start empty and then overwrite the bad file on the next
            # write — preserve it and say so, so the org can be recovered by hand.
            self._quarantine(f"unreadable or corrupt ({e})")
            return
        version = raw.get("version", 1)
        if version > SCHEMA_VERSION:
            self._quarantine(
                f"written by a newer schema (v{version} > v{SCHEMA_VERSION}); "
                f"refusing to load it with older code")
            return
        self.orchestrator_thread = raw.get("orchestrator_thread")
        self.dashboard_msg_id = raw.get("dashboard_msg_id")
        self.pending_delivery = dict(raw.get("pending_delivery") or {})
        self.office_message_ids = {
            str(k): [int(i) for i in v]
            for k, v in (raw.get("office_message_ids") or {}).items()
            if isinstance(v, list)}
        # Restore every field the current schema knows about (ignoring any it no
        # longer has). Enumerating by hand here silently dropped base_branch/base_sha
        # on restart once — deriving from the dataclass means new fields persist for
        # free and delivery targeting survives a reboot.
        known = {f.name for f in dataclass_fields(ThreadRecord)}
        for k, v in raw.get("threads", {}).items():
            if not isinstance(v, dict):
                continue
            filtered = {kk: vv for kk, vv in v.items() if kk in known}
            filtered.setdefault("role", "direct")
            filtered.setdefault("name", "")
            self._threads[k] = ThreadRecord(**filtered)

    def _quarantine(self, why: str) -> None:
        log.error("core state %s — starting fresh: %s", self.path.name, why)
        try:
            backup = self.path.with_name(f"core.json.corrupt-{int(time.time())}")
            os.replace(self.path, backup)
            log.error("previous state preserved at %s (recover by hand if needed)", backup)
        except OSError:
            pass

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        payload = {
            "version": SCHEMA_VERSION,
            "orchestrator_thread": self.orchestrator_thread,
            "dashboard_msg_id": self.dashboard_msg_id,
            "pending_delivery": self.pending_delivery,
            "office_message_ids": self.office_message_ids,
            "threads": {k: asdict(v) for k, v in self._threads.items()},
        }
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            # Persistence failed (disk full / read-only): keep running on the
            # in-memory state and log loudly, rather than crashing the loop.
            log.error("could not persist core state (kept in memory): %s", e)

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

    def set_dashboard_msg_id(self, mid: int | None) -> None:
        if self.dashboard_msg_id != mid:
            self.dashboard_msg_id = mid
            self._flush()

    def set_pending_delivery(self, pending: dict[str, str]) -> None:
        """Persist the awaiting-/approve set — an approval must survive a restart."""
        if self.pending_delivery != pending:
            self.pending_delivery = dict(pending)
            self._flush()

    def record_office_message(self, chat_id: int, message_id: int) -> None:
        """Remember a message in an office chat so a factory reset can delete it.
        Bounded per chat — the oldest ids drop once past the cap."""
        ids = self.office_message_ids.setdefault(str(chat_id), [])
        ids.append(int(message_id))
        if len(ids) > OFFICE_MSG_CAP:
            del ids[:-OFFICE_MSG_CAP]
        self._flush()

    def clear_office_messages(self) -> None:
        if self.office_message_ids:
            self.office_message_ids = {}
            self._flush()

    def wipe(self) -> None:
        """Factory reset: forget every thread, the office, and the dashboard."""
        self._threads.clear()
        self.orchestrator_thread = None
        self.dashboard_msg_id = None
        self.pending_delivery = {}
        self.office_message_ids = {}
        self._flush()

    def workers(self) -> dict[str, ThreadRecord]:
        return {k: v for k, v in self._threads.items() if v.role == "worker"}
