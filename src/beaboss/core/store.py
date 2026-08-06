"""Restart-proof state: thread registry + fleet records. One JSON file, atomic
rewrite on change (small data, single event loop — same approach as before).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
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
BOSS_INBOX_CAP = 100


@dataclass
class ThreadRecord:
    """One thread the core knows about."""

    role: Role
    name: str
    cwd: str = ""            # repo (direct) or worktree (worker); "" = none yet
    session_id: str | None = None
    # Native sessions are not portable across harnesses. Keep one id per provider;
    # session_id remains the active/legacy compatibility field.
    backend: str = ""
    session_ids: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0
    # worker-only:
    worker_id: str = ""       # short id, e.g. "nova"
    repo: str = ""           # the primary checkout the worktree came from
    base_branch: str = ""    # the branch the worker forked from (merge/PR target)
    checks: str = ""         # last run_checks verdict: "" | pass | fail
    checks_sha: str = ""     # branch tip when checks last ran (to detect staleness)
    task: str = ""           # the brief, verbatim
    worker_status: str = ""   # working | done | blocked | dismissed | delivered
    tier: str = ""            # requested routing tier: fast | balanced | deep
    model: str = ""          # resolved model id for this worker ("" = global default)
    models: dict[str, str] = field(default_factory=dict)
    reasoning_effort: str = ""  # resolved effort for the active backend
    reasoning_efforts: dict[str, str] = field(default_factory=dict)
    # project-manager hierarchy (additive; blank fields preserve legacy/direct work):
    manager_id: str = ""       # manager-only stable id (normally the repo slug)
    manager_status: str = ""   # manager-only: active | dismissed
    supervisor_id: str = ""    # worker-only: owning manager_id; blank = orchestrator
    last_summary: str = ""     # manager's latest bounded portfolio-level report
    project_id: str = ""       # owning durable project; blank = independent/legacy
    # Version of the dynamic-tool schema embedded in the Codex native session.
    # Zero is legacy/unknown; used to rotate sessions whose resume protocol
    # cannot accept updated dynamic tool definitions.
    tool_schema_version: int = 0


@dataclass
class ProjectRecord:
    """A durable outcome/context boundary, independent of repository layout."""

    project_id: str
    name: str
    charter: str
    repos: list[str] = field(default_factory=list)
    status: str = "active"      # active | blocked | completed | archived
    manager_id: str = ""
    manager_thread: str = ""
    last_summary: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


class CoreStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "core.json"
        self.organization_path = self.state_dir / "organization.json"
        self._threads: dict[str, ThreadRecord] = {}
        self._projects: dict[str, ProjectRecord] = {}
        self.orchestrator_thread: str | None = None
        # Where the boss last spoke to the orchestrator (#general or a DM), so
        # asynchronous supervision replies continue in the same conversation after
        # a restart instead of unexpectedly jumping back to #general.
        self.last_boss_thread: str | None = None
        self.dashboard_msg_id: int | None = None   # the pinned #general status board
        # worker_id -> {method, sha, base_sha}, awaiting a revision-bound /approve.
        # Legacy state may still contain a plain method string and is refused safely.
        self.pending_delivery: dict[str, dict[str, str] | str] = {}
        # Message ids in the orchestrator's offices (#general + DMs), keyed by chat id.
        # Worker topics are deleted wholesale on reset; these have no topic to drop, so
        # a factory reset deletes them by id. Bounded so it can't grow without limit.
        self.office_message_ids: dict[str, list[int]] = {}
        # Boss turns remain here until the orchestrator backend succeeds. This
        # closes the gap where a restart or expired credential consumed the chat
        # update but lost the request from the in-memory session queue.
        self.pending_boss_turns: list[dict[str, str]] = []
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
        self.last_boss_thread = raw.get("last_boss_thread")
        self.dashboard_msg_id = raw.get("dashboard_msg_id")
        self.pending_delivery = dict(raw.get("pending_delivery") or {})
        self.office_message_ids = {
            str(k): [int(i) for i in v]
            for k, v in (raw.get("office_message_ids") or {}).items()
            if isinstance(v, list)}
        self.pending_boss_turns = [
            {"id": str(v["id"]), "thread_id": str(v["thread_id"]),
             "text": str(v["text"])}
            for v in (raw.get("pending_boss_turns") or [])
            if isinstance(v, dict)
            and all(k in v for k in ("id", "thread_id", "text"))
        ][-BOSS_INBOX_CAP:]
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
        project_known = {f.name for f in dataclass_fields(ProjectRecord)}
        for project_id, value in raw.get("projects", {}).items():
            if not isinstance(value, dict):
                continue
            filtered = {k: v for k, v in value.items() if k in project_known}
            filtered.setdefault("project_id", str(project_id))
            filtered.setdefault("name", str(project_id))
            filtered.setdefault("charter", "")
            self._projects[str(project_id)] = ProjectRecord(**filtered)
        self._migrate_legacy_projects()

    def _migrate_legacy_projects(self) -> None:
        """Lift repo-bound manager records into outcome projects without losing IDs."""
        changed = False
        for thread_id, manager in self.managers().items():
            project_id = manager.project_id or manager.manager_id
            if not project_id:
                continue
            if project_id not in self._projects:
                now = manager.created_at or time.time()
                self._projects[project_id] = ProjectRecord(
                    project_id=project_id,
                    name=Path(manager.repo).name if manager.repo else manager.name,
                    charter=manager.task,
                    repos=[manager.repo] if manager.repo else [],
                    status=("archived" if manager.manager_status == "dismissed"
                            else "active"),
                    manager_id=manager.manager_id,
                    manager_thread=thread_id,
                    last_summary=manager.last_summary,
                    created_at=now,
                    updated_at=now,
                )
                changed = True
            if manager.project_id != project_id:
                manager.project_id = project_id
                changed = True
            for worker in self._threads.values():
                if (worker.role == "worker"
                        and worker.supervisor_id == manager.manager_id
                        and not worker.project_id):
                    worker.project_id = project_id
                    changed = True
        if changed:
            self._flush()

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
            "last_boss_thread": self.last_boss_thread,
            "dashboard_msg_id": self.dashboard_msg_id,
            "pending_delivery": self.pending_delivery,
            "office_message_ids": self.office_message_ids,
            "pending_boss_turns": self.pending_boss_turns,
            "threads": {k: asdict(v) for k, v in self._threads.items()},
            "projects": {k: asdict(v) for k, v in self._projects.items()},
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

    def projects(self) -> dict[str, ProjectRecord]:
        return dict(self._projects)

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

    def put_project(self, project: ProjectRecord) -> None:
        now = time.time()
        if not project.created_at:
            project.created_at = now
        project.updated_at = now
        self._projects[project.project_id] = project
        self._flush()

    def update_project(self, project_id: str, **fields) -> None:
        project = self._projects.get(project_id)
        if project is None:
            return
        changed = False
        for key, value in fields.items():
            if getattr(project, key, None) != value:
                setattr(project, key, value)
                changed = True
        if changed:
            project.updated_at = time.time()
            self._flush()

    def delete_project(self, project_id: str) -> None:
        if self._projects.pop(project_id, None) is not None:
            self._flush()

    def write_organization(self, organization: dict) -> None:
        """Publish the code-owned org view for read-only observer surfaces.

        This deliberately lives beside, rather than inside, ``core.json`` so a
        dashboard can mount state read-only without parsing mutable engine state.
        The atomic replace also means readers never observe a partial document.
        """
        tmp = self.organization_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(organization, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self.organization_path)
        except OSError as e:
            log.error("could not publish organization snapshot: %s", e)

    def set_orchestrator_thread(self, thread_id: str | None) -> None:
        if self.orchestrator_thread != thread_id:
            self.orchestrator_thread = thread_id
            self._flush()

    def set_last_boss_thread(self, thread_id: str | None) -> None:
        if self.last_boss_thread != thread_id:
            self.last_boss_thread = thread_id
            self._flush()

    def set_dashboard_msg_id(self, mid: int | None) -> None:
        if self.dashboard_msg_id != mid:
            self.dashboard_msg_id = mid
            self._flush()

    def set_pending_delivery(
        self, pending: dict[str, dict[str, str] | str],
    ) -> None:
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

    def enqueue_boss_turn(self, thread_id: str, text: str) -> str:
        turn_id = uuid.uuid4().hex
        self.pending_boss_turns.append({
            "id": turn_id, "thread_id": thread_id, "text": text})
        if len(self.pending_boss_turns) > BOSS_INBOX_CAP:
            dropped = len(self.pending_boss_turns) - BOSS_INBOX_CAP
            del self.pending_boss_turns[:dropped]
            log.error("boss inbox exceeded %d turns; dropped %d oldest",
                      BOSS_INBOX_CAP, dropped)
        self._flush()
        return turn_id

    def acknowledge_boss_turns(self, turn_ids: list[str]) -> None:
        ids = set(turn_ids)
        if not ids:
            return
        kept = [turn for turn in self.pending_boss_turns if turn["id"] not in ids]
        if len(kept) != len(self.pending_boss_turns):
            self.pending_boss_turns = kept
            self._flush()

    def wipe(self) -> None:
        """Factory reset: forget every thread, the office, and the dashboard."""
        self._threads.clear()
        self.orchestrator_thread = None
        self.last_boss_thread = None
        self.dashboard_msg_id = None
        self.pending_delivery = {}
        self.office_message_ids = {}
        self.pending_boss_turns = []
        self._projects.clear()
        self._flush()

    def workers(self) -> dict[str, ThreadRecord]:
        return {k: v for k, v in self._threads.items() if v.role == "worker"}

    def managers(self) -> dict[str, ThreadRecord]:
        return {
            k: v for k, v in self._threads.items()
            if v.role == "project_manager"
        }
