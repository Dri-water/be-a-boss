"""The engine: owns all sessions, routes messages, exposes the orchestrator.

Threads and roles:
- orchestrator thread ("the office", transport's main thread): human <-> orchestrator
- worker threads: a visible pair — the orchestrator drives a worker session, and
  everything both say is posted to the thread. The human may interject; the
  message reaches the worker as input and the orchestrator via its inbox.
- direct threads: the original beaboss model (human <-> session), unchanged.

Supervision is checkpoint-based: worker turn-ends and human interjections land in
an inbox; each worker turn-end wakes the orchestrator with the accumulated digest.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_backend import AgentResult, AgentTool, ToolNamespace
from .names import pick_name, worker_id_for
from .prompts import (DELIVERY_BALANCED, DELIVERY_CONSERVATIVE,
                      ORCHESTRATOR_APPEND, PROJECT_MANAGER_APPEND,
                      WORKER_APPEND_EXTRA)
from .ports import (InboundMessage, MediaIn, Outbound, OutboundDeliveryError,
                    Speaker, SYSTEM, Transport)
from .session import CoreSession, DEFAULT_SESSION_APPEND
from .recovery import infer_legacy_backend, recovery_append
from .store import CoreStore, ProjectRecord, ThreadRecord
from . import worktrees

log = logging.getLogger("beaboss.core.engine")

# Codex persists dynamic tools inside native thread metadata and cannot override
# them on resume. Bump this whenever orchestrator/manager tool names or schemas
# change; legacy native sessions rotate once with a code-owned recovery handoff.
ORG_TOOL_SCHEMA_VERSION = 2

ORCHESTRATOR_EMOJI = "🧭"
PROJECT_MANAGER_EMOJI = "🗂️"
WORKER_EMOJI = "⚙️"
MAX_INBOX = 200  # bound the supervision backlog if the orchestrator can't drain it


@dataclass(frozen=True)
class DeliveryPlan:
    thread_id: str
    rec: ThreadRecord
    repo: Path
    worktree: Path
    branch: str
    base: str
    tip: str
    base_tip: str
    checks_note: str


_EFFORT_RANK = {
    "none": 0, "minimal": 0, "low": 1, "medium": 2,
    "high": 3, "xhigh": 4, "max": 5, "ultra": 6,
}


def _routing_warnings(settings) -> list[str]:
    """Detect expensive or collapsed routing before it silently reaches workers."""
    if settings.agent_backend != "codex":
        return []
    profiles = settings.worker_profiles()
    warnings: list[str] = []
    if len(set(profiles.values())) == 1:
        warnings.append("all worker tiers resolve to the same model and effort")
    for tier in ("fast", "balanced", "deep"):
        model, effort = profiles[tier]
        if not model or not effort:
            warnings.append(
                f"{tier} has no explicit model/effort and may inherit global defaults")
    fast_effort = (profiles["fast"][1] or "").lower()
    balanced_effort = (profiles["balanced"][1] or "").lower()
    if _EFFORT_RANK.get(fast_effort, -1) >= _EFFORT_RANK["high"]:
        warnings.append(f"fast uses expensive reasoning effort '{fast_effort}'")
    if _EFFORT_RANK.get(balanced_effort, -1) > _EFFORT_RANK["high"]:
        warnings.append(f"balanced uses unusually expensive effort '{balanced_effort}'")
    ranks = [_EFFORT_RANK.get((profiles[t][1] or "").lower(), -1)
             for t in ("fast", "balanced", "deep")]
    if min(ranks) >= 0 and ranks != sorted(ranks):
        warnings.append("reasoning effort decreases as task difficulty increases")
    return warnings


def _routing_report(settings) -> str:
    lines = [f"backend: {settings.agent_backend}", "worker routing:"]
    for tier, (model, effort) in settings.worker_profiles().items():
        lines.append(
            f"- {tier}: model={model or '(backend default)'}; "
            f"effort={effort or '(backend default)'}")
    warnings = _routing_warnings(settings)
    lines.append("health: " + ("; ".join(warnings) if warnings else "healthy"))
    return "\n".join(lines)



# --- repo grounding (so the orchestrator manages from knowledge, not vibes) ----


def _read_doc(path: Path, limit: int) -> str:
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return (text[:limit] + "\n…(truncated — read the file directly for more)…"
            if len(text) > limit else text)


def _repo_hint(repo: Path) -> str:
    """One line describing a repo, for the repo list — first real line of its guide."""
    for doc in ("AGENTS.md", "README.md"):
        text = _read_doc(repo / doc, limit=600)
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line and not line.startswith("![") and not line.startswith("<"):
                return line[:140]
    return ""


def _top_level(repo: Path) -> str:
    try:
        entries = sorted(repo.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as e:
        return f"(couldn't list: {e})"
    rows = []
    for p in entries:
        if p.name.startswith(".") and p.name != ".github":
            continue
        rows.append(f"- {p.name}/" if p.is_dir() else f"- {p.name}")
    return "\n".join(rows[:40]) or "(empty)"


def _parse_worker_status(text: str) -> str | None:
    """The worker's self-reported status, read from the LAST STATUS: line — tolerating
    markdown decoration (**bold**, '- ', '> ') and a trailing courtesy line after it.
    A quote of the STATUS menu (contains '|') is skipped, and the last real status
    wins, so quoting the protocol mid-reply can't be misread as done."""
    for raw in reversed(text.splitlines()):
        line = raw.strip().strip("*_> ").lstrip("-").strip().lower()
        if not line.startswith("status:"):
            continue
        value = line.split("status:", 1)[1].strip()
        if "|" in value:
            continue  # the protocol menu, not a real status
        if value.startswith("done"):
            return "done"
        if value.startswith(("blocked", "needs-decision")):
            return "blocked"
        if value.startswith("working"):
            return "working"
        return None  # an unrecognized status line — don't scan past it
    return None


def _test_hint(repo: Path) -> str:
    """A best-guess check command from the repo's shape. A HINT — the orchestrator
    still verifies by actually running it via run_checks on a worker."""
    if (repo / "pyproject.toml").is_file():
        return "uv run pytest"
    if (repo / "package.json").is_file():
        return "npm test"
    if (repo / "Cargo.toml").is_file():
        return "cargo test"
    if (repo / "go.mod").is_file():
        return "go test ./..."
    if (repo / "Makefile").is_file():
        return "make test"
    return ""


class Engine:
    DELIVERY_MAX_ATTEMPTS = 3
    DELIVERY_RETRY_BASE = 0.5

    def __init__(self, settings, store: CoreStore):
        self.settings = settings
        self.store = store
        self.transport: Transport | None = None
        self.sessions: dict[str, CoreSession] = {}
        # Done/delivered workers remain resumable from their persisted native id,
        # but their local app-server and descendant dev servers must not consume
        # resources indefinitely. Retirement runs just after the completing turn
        # unwinds, then _ensure_session lazily reattaches if follow-up arrives.
        self._retirement_tasks: set[asyncio.Task] = set()
        self._inbox: list[str] = []          # pending supervision notes for the orchestrator
        self._manager_inboxes: dict[str, list[str]] = {}
        self._manager_waking: set[str] = set()
        self._pending_vision: list[str] = []  # recent worker screenshots to show the orchestrator
        self._waking = False                 # digest wake in flight
        self._session_locks: dict[str, asyncio.Lock] = {}  # per-thread start lock
        # worker_id -> {method, sha, base_sha}, awaiting /approve. Restored so the
        # exact reviewed source and target remain approvable after a restart.
        self._pending_delivery: dict[str, dict[str, str] | str] = dict(
            store.pending_delivery)
        self._last_dashboard = ""                          # last rendered board (skip no-op edits)
        self._last_organization: dict[str, Any] | None = None
        # Where the boss last spoke to the orchestrator (#general or a DM). Digest
        # replies and approval prompts follow the boss there instead of stranding
        # the conversation in #general while their DM goes silent.
        self._last_boss_thread = store.last_boss_thread or "general"
        # Fleet actions taken during the current orchestrator turn — drained into a
        # code-generated footer on its reply, so the boss can SEE what actually
        # happened (a claim with no matching ⚙ line is visibly false).
        self._turn_actions: list[str] = []
        # The thread that is the orchestrator's "office". Transports may override;
        # the Telegram adapter's General topic maps to "general".
        self.main_thread = "general"
        for warning in _routing_warnings(settings):
            log.warning("agent routing configuration: %s", warning)

    # ---- wiring ----------------------------------------------------------

    def attach_transport(self, transport: Transport) -> None:
        self.transport = transport

    async def _post(self, out: Outbound) -> None:
        assert self.transport is not None
        # Audit trail: log what the orchestrator actually SAYS. Its replies/decisions
        # were previously unlogged, which made a run impossible to review after the fact.
        if out.speaker.role == "orchestrator" and out.text.strip():
            log.info("orchestrator -> %s: %s", out.thread_id, out.text.strip()[:300])
        # Give the orchestrator EYES: remember a worker's screenshots so its next
        # supervision turn can SEE the work and judge it, not just read a description.
        # Detect images by the actual file type, not the media_kind label.
        if out.media_path is not None:
            rec = self.store.get(out.thread_id)
            if rec is not None and rec.role == "worker":
                mime, _ = mimetypes.guess_type(str(out.media_path))
                if (mime or "").startswith("image/"):
                    self._pending_vision.append(str(out.media_path))
                    self._pending_vision = self._pending_vision[-6:]  # keep the recent few
        for attempt in range(1, self.DELIVERY_MAX_ATTEMPTS + 1):
            try:
                await self.transport.post(out)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if attempt >= self.DELIVERY_MAX_ATTEMPTS:
                    raise OutboundDeliveryError(
                        f"could not deliver output to {out.thread_id} after "
                        f"{attempt} attempts: {e}") from e
                delay = self.DELIVERY_RETRY_BASE * 2 ** (attempt - 1)
                log.warning(
                    "output delivery failed thread=%s attempt=%d/%d; retrying in %.1fs: %s",
                    out.thread_id, attempt, self.DELIVERY_MAX_ATTEMPTS, delay, e)
                await asyncio.sleep(delay)

    def _vision_items(self, paths: list[str]) -> list[MediaIn]:
        """Turn recent worker screenshot paths into MediaIn for the orchestrator to see.
        Skips anything gone/unreadable/non-image — a missing screenshot must never
        break a wake."""
        items: list[MediaIn] = []
        for p in paths:
            try:
                data = Path(p).read_bytes()
            except OSError:
                continue
            mime, _ = mimetypes.guess_type(p)
            if not (mime or "").startswith("image/"):
                continue
            items.append(MediaIn(kind="image", filename=Path(p).name, mime=mime, data=data))
        return items

    async def _busy(self, thread_id: str) -> None:
        if self.transport is not None:
            await self.transport.indicate_busy(thread_id)
        await self._refresh_organization()

    async def _idle(self, thread_id: str) -> None:
        # The turn-end counterpart to _busy. Optional on the transport: Telegram's
        # typing indicator self-expires, so it needs no idle; the web/CLI cockpits
        # hold "working" until told otherwise, so they do.
        indicate_idle = getattr(self.transport, "indicate_idle", None)
        if indicate_idle is not None:
            await indicate_idle(thread_id)
        await self._refresh_organization()

    # ---- speakers --------------------------------------------------------

    def orchestrator_speaker(self) -> Speaker:
        return Speaker(role="orchestrator", name=self.settings.bot_name,
                       emoji=ORCHESTRATOR_EMOJI)

    @staticmethod
    def project_manager_speaker(name: str) -> Speaker:
        return Speaker(role="project_manager", name=name,
                       emoji=PROJECT_MANAGER_EMOJI)

    @staticmethod
    def worker_speaker(name: str) -> Speaker:
        return Speaker(role="worker", name=name, emoji=WORKER_EMOJI)

    def _is_orchestrator_thread(self, thread_id: str) -> bool:
        """Where you talk to the (single) orchestrator: the shared #general, or any
        DM. Both drive the same one orchestrator; its reply goes back to whichever
        you used, so a DM keeps chatter out of #general — no separate 'office'."""
        return thread_id == self.main_thread or thread_id.startswith("dm:")

    def _fleet_snapshot(self) -> str:
        """One compact line of ground truth, injected into every boss turn."""
        rows = []
        for _tid, manager in self.store.managers().items():
            if manager.manager_status == "dismissed":
                continue
            project = self._project_for_manager(manager)
            active = sum(
                1 for worker in self.store.workers().values()
                if (worker.project_id == (project.project_id if project else "")
                    or worker.supervisor_id == manager.manager_id)
                and worker.worker_status not in ("dismissed", "delivered")
            )
            rows.append(
                f"project {(project.name if project else Path(manager.repo).name)}="
                f"{(project.project_id if project else manager.manager_id)}/"
                f"{active} active workers")
        for tid, rec in self.store.workers().items():
            if rec.worker_status in ("dismissed", "delivered"):
                continue  # terminal — not "right now" work; keeps the line bounded
            if rec.supervisor_id:
                continue
            live = self.sessions.get(tid)
            run = live.status if live else "dormant"
            state = rec.worker_status or "working"
            if rec.worker_id in self._pending_delivery:
                state += ", awaiting /approve"
            rows.append(f"{rec.worker_id}={state}/{run} on {Path(rec.repo).name}")
        return "; ".join(rows) if rows else "no workers exist"

    def _action(self, line: str) -> None:
        """Record a fleet action for the current orchestrator turn's footer."""
        self._turn_actions.append(line)
        log.info("orchestrator action: %s", line)   # audit trail of what it actually DID

    def _drain_turn_actions(self) -> str | None:
        """The code-generated footer for the orchestrator's reply: what it actually
        DID this turn. None (no footer) when no fleet tools ran — so the absence of
        a ⚙ line is itself information."""
        acts, self._turn_actions = self._turn_actions, []
        return ("⚙ " + " · ".join(acts)) if acts else None

    # ---- inbound routing -------------------------------------------------

    async def on_inbound(self, msg: InboundMessage) -> None:
        # Talking to the orchestrator (#general or any DM) → the one orchestrator
        # session, replying back to wherever you spoke.
        if self._is_orchestrator_thread(msg.thread_id):
            # Persist the conversation target before starting the backend. Even if
            # session startup itself fails, later recovery still belongs where the
            # boss actually spoke rather than falling back to an older surface.
            self._last_boss_thread = msg.thread_id
            self.store.set_last_boss_thread(msg.thread_id)
            delivery_id = self.store.enqueue_boss_turn(msg.thread_id, msg.text)
            await self._ensure_orchestrator(self.main_thread)
            session = await self._ensure_session(
                self.main_thread, self.store.get(self.main_thread))
            if session is None:
                return
            # Ground every boss turn in reality: a code-generated snapshot of the
            # actual fleet rides along with the message, so the orchestrator can't
            # honestly claim "Nova is on it" when no one is.
            text = f"[fleet right now: {self._fleet_snapshot()}]\n{msg.text}"
            if msg.media:
                await session.submit_media(
                    text, msg.media, reply_to=msg.thread_id,
                    delivery_ids=[delivery_id])
            else:
                await session.submit(
                    text, reply_to=msg.thread_id, delivery_ids=[delivery_id])
            return

        rec = self.store.get(msg.thread_id)
        if rec is None:
            await self._post(Outbound(
                thread_id=msg.thread_id, speaker=SYSTEM,
                text="This thread isn't active. Talk to the orchestrator in #general "
                     "or a DM, or use /new for a direct session.",
            ))
            return

        if rec.role == "worker" and rec.worker_status == "dismissed":
            # Its worktree was torn down at dismiss; don't resurrect it into a gone
            # workspace (or bring a dismissed worker back to life).
            await self._post(Outbound(
                thread_id=msg.thread_id, speaker=SYSTEM,
                text=(f"{rec.name} was dismissed — its workspace is gone (any work is "
                      f"on branch worker/{rec.worker_id}). Ask the orchestrator to "
                      f"hire a fresh worker."),
            ))
            return

        if rec.role == "worker":
            project = self._find_project(rec.project_id) if rec.project_id else None
            if rec.worker_status == "delivered" or (
                    project and project.status in ("completed", "archived")):
                await self._post(Outbound(
                    thread_id=msg.thread_id, speaker=SYSTEM,
                    text=(f"{rec.name}'s task is closed and its delivered revision is "
                          "immutable here. Ask the orchestrator to reactivate the "
                          "project if needed and hire a fresh worker."),
                ))
                return

        if rec.role == "project_manager" and rec.manager_status == "dismissed":
            await self._post(Outbound(
                thread_id=msg.thread_id, speaker=SYSTEM,
                text=(f"{rec.name} was dismissed. Ask the orchestrator to reopen "
                      f"management for {Path(rec.repo).name}."),
            ))
            return

        if rec.role == "project_manager":
            project = self._project_for_manager(rec)
            if project and project.status == "archived":
                await self._post(Outbound(
                    thread_id=msg.thread_id, speaker=SYSTEM,
                    text=(f"Project {project.name} is archived. Ask the orchestrator "
                          "to create a new project for renewed work."),
                ))
                return
            if project and project.status == "completed":
                # A direct boss interjection is explicit authority to reopen the
                # completed project; agent-to-agent tools still require reactivation.
                self.store.update_project(project.project_id, status="active")
                await self._refresh_dashboard()

        session = await self._ensure_session(msg.thread_id, rec)
        if session is None:
            return

        if rec.role == "worker":
            await self._interject(msg, rec, session)
            return

        if rec.role == "project_manager":
            who = msg.sender_name or "the boss"
            text = f"[Direct project-room message from {who}]: {msg.text}"
            if msg.media:
                await session.submit_media(text, msg.media)
            else:
                await session.submit(text)
            return

        # direct session: plain turn
        if msg.media:
            await session.submit_media(msg.text, msg.media)
        else:
            await session.submit(msg.text)

    async def _interject(self, msg: InboundMessage, rec: ThreadRecord,
                         session: CoreSession) -> None:
        """Boss speaks in a worker thread; its immediate supervisor is notified."""
        who = msg.sender_name or "the boss"
        supervisor = "your project manager" if rec.supervisor_id else "the orchestrator"
        text = (f"[Interjection from {who} — visible to you and {supervisor}]: "
                f"{msg.text}")
        # a boss follow-up un-sticks a done/blocked marker (the worker is back at it)
        if rec.worker_status in ("done", "blocked"):
            self.store.update(msg.thread_id, worker_status="working")
        if msg.media:
            await session.submit_media(text, msg.media)
        else:
            await session.submit(text)
        note = f"{who} said in {rec.worker_id}'s thread: {msg.text}"
        manager = self._find_manager(rec.supervisor_id) if rec.supervisor_id else None
        if manager:
            self._manager_note(manager[0], note)
        else:
            self._note(note)

    # ---- session management ---------------------------------------------

    async def _ensure_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession | None:
        session = self.sessions.get(thread_id)
        if session is not None and session.alive:
            return session
        # Serialize per thread: two coroutines (a transport handler + a worker's wake,
        # or two fleet calls) must not both build a session for one thread — the
        # loser's live subprocess would leak untracked. Also the point where we
        # replace a zombie (dead run task / failed reconnect) instead of reusing it.
        lock = self._session_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            session = self.sessions.get(thread_id)
            if session is not None and session.alive:
                return session
            if session is not None:                    # a zombie/errored session
                self.sessions.pop(thread_id, None)
                try:
                    await session.stop()
                except Exception:  # noqa: BLE001
                    pass
            try:
                if rec.role == "orchestrator":
                    session = self._make_orchestrator_session(thread_id, rec)
                elif rec.role == "project_manager":
                    session = self._make_project_manager_session(thread_id, rec)
                elif rec.role == "worker":
                    session = self._make_worker_session(thread_id, rec)
                else:
                    session = self._make_direct_session(thread_id, rec)
                await session.start()
            except Exception as e:  # noqa: BLE001
                log.exception("failed to start session thread=%s", thread_id)
                await self._post(Outbound(
                    thread_id=thread_id, speaker=SYSTEM,
                    text=f"⚠️ couldn't start this session: {e}",
                ))
                return None
            self.sessions[thread_id] = session
            return session

    def _sid_saver(self, thread_id: str, *, tool_schema_version: int = 0):
        backend = self.settings.agent_backend

        def save(sid: str) -> None:
            rec = self.store.get(thread_id)
            ids = dict(rec.session_ids) if rec else {}
            ids[backend] = sid
            fields: dict[str, Any] = {
                "backend": backend, "session_id": sid, "session_ids": ids}
            if tool_schema_version and backend == "codex":
                fields["tool_schema_version"] = tool_schema_version
            self.store.update(thread_id, **fields)
        return save

    def _rotate_stale_codex_tools(
        self, rec: ThreadRecord, resume_id: str | None, handoff: str,
    ) -> tuple[str | None, str]:
        """Start fresh when a resumed Codex thread embeds an older tool schema."""
        if (self.settings.agent_backend != "codex" or not resume_id
                or rec.tool_schema_version >= ORG_TOOL_SCHEMA_VERSION):
            return resume_id, handoff
        upgrade = (
            "\n\n[DYNAMIC TOOL SCHEMA UPGRADE — code-generated recovery]\n"
            "The previous Codex native thread used an older fleet/project tool schema "
            "that cannot be replaced during thread/resume. This is a fresh native "
            "session with current tools. Recover from the durable project, worker, "
            "workspace, git, and pending-turn state supplied by be-a-boss; do not "
            "invent missing history or repeat external side effects.\n")
        return None, handoff + upgrade

    def _session_material(
        self, thread_id: str, rec: ThreadRecord,
    ) -> tuple[str | None, str | None, str | None, str]:
        """Resolve provider-native session/profile data and any lossy hand-off text."""
        desired = self.settings.agent_backend
        ids = dict(rec.session_ids)
        models = dict(rec.models)
        efforts = dict(rec.reasoning_efforts)
        source = rec.backend
        if rec.session_id:
            if not source:
                source = infer_legacy_backend(rec.session_id)
            ids.setdefault(source, rec.session_id)
        if rec.model:
            # A pre-provider-tag record with no native session yet was necessarily
            # created for the then-active backend; treat the configured backend as
            # its owner. Records with a session are inferred above.
            models.setdefault(source or desired, rec.model)
        if rec.reasoning_effort:
            efforts.setdefault(source or desired, rec.reasoning_effort)

        resume_id = ids.get(desired) or None
        model = models.get(desired) or self.settings.model
        effort = efforts.get(desired) or self.settings.reasoning_effort
        handoff = ""
        source_with_session = (
            source if source and source != desired and ids.get(source) else
            (next((name for name, sid in ids.items()
                   if name != desired and sid), "") if not resume_id else "")
        )
        if source_with_session:
            handoff = recovery_append(
                source_with_session, ids[source_with_session],
                role=rec.role, task=rec.task)

        self.store.update(
            thread_id,
            backend=desired,
            session_id=resume_id,
            session_ids=ids,
            model=model or "",
            models=models,
            reasoning_effort=effort or "",
            reasoning_efforts=efforts,
        )
        return resume_id, model, effort, handoff

    def _make_direct_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession:
        resume_id, model, effort, handoff = self._session_material(thread_id, rec)
        return CoreSession(
            thread_id=thread_id, cwd=Path(rec.cwd),
            speaker=Speaker(role="direct", name=self.settings.bot_name),
            settings=self.settings, post=self._post, busy=self._busy, idle=self._idle,
            on_session_id=self._sid_saver(thread_id), session_id=resume_id,
            system_append=DEFAULT_SESSION_APPEND + handoff,
            model_override=model,
            reasoning_effort_override=effort,
        )

    def _make_orchestrator_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession:
        resume_id, model, effort, handoff = self._session_material(thread_id, rec)
        resume_id, handoff = self._rotate_stale_codex_tools(rec, resume_id, handoff)
        home = self.settings.state_dir / "orchestrator-home"
        home.mkdir(parents=True, exist_ok=True)
        base = (self.settings.session_system_append
                if self.settings.session_system_append is not None else "")
        mode = (DELIVERY_CONSERVATIVE
                if self.settings.deploy_braveness == "conservative" else DELIVERY_BALANCED)
        routing = (
            "\n\nRUNTIME AGENT ROUTING (code-generated ground truth):\n"
            + _routing_report(self.settings)
            + "\nChoose fast/balanced/deep deliberately when spawning. Use "
              "routing_status whenever routing looks collapsed, unexpectedly costly, "
              "or a worker reports a model/effort problem.\n")
        append = ((base + "\n\n" if base else "")
                  + ORCHESTRATOR_APPEND + mode + routing + handoff)
        session = CoreSession(
            thread_id=thread_id, cwd=home,
            speaker=self.orchestrator_speaker(),
            settings=self.settings, post=self._post, busy=self._busy, idle=self._idle,
            on_session_id=self._sid_saver(
                thread_id, tool_schema_version=ORG_TOOL_SCHEMA_VERSION),
            session_id=resume_id,
            system_append=append,
            extra_tool_namespaces=(self._build_fleet_tools(),),
            final_only=True,  # text the boss one clean reply, don't narrate
            footer_fn=self._drain_turn_actions,  # …plus a truthful ⚙ action line
            model_override=model,
            reasoning_effort_override=effort,
        )
        session.on_turn_done = self._on_orchestrator_turn_done
        session.on_turn_error = self._on_orchestrator_turn_error
        return session

    def _make_worker_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession:
        resume_id, model, effort, handoff = self._session_material(thread_id, rec)
        cwd = Path(rec.cwd)
        # The worker's full system prompt = the base env note + the worker role note.
        base = (self.settings.session_system_append
                if self.settings.session_system_append is not None
                else DEFAULT_SESSION_APPEND)
        worker_append = base + WORKER_APPEND_EXTRA + handoff
        session = CoreSession(
            thread_id=thread_id, cwd=cwd,
            speaker=self.worker_speaker(rec.name),
            settings=self.settings, post=self._post, busy=self._busy, idle=self._idle,
            on_session_id=self._sid_saver(thread_id), session_id=resume_id,
            system_append=worker_append,
            model_override=model,
            reasoning_effort_override=effort,
        )
        session.on_turn_done = self._on_worker_turn_done
        session.on_turn_error = self._on_worker_turn_error
        return session

    def _make_project_manager_session(
        self, thread_id: str, rec: ThreadRecord,
    ) -> CoreSession:
        resume_id, model, effort, handoff = self._session_material(thread_id, rec)
        resume_id, handoff = self._rotate_stale_codex_tools(rec, resume_id, handoff)
        home = Path(rec.cwd)
        home.mkdir(parents=True, exist_ok=True)
        base = (self.settings.session_system_append
                if self.settings.session_system_append is not None else "")
        project_record = self._project_for_manager(rec)
        repos = (project_record.repos if project_record else
                 ([rec.repo] if rec.repo else []))
        charter = project_record.charter if project_record else rec.task
        project = (
            "\n\nCODE-GENERATED PROJECT SCOPE (cannot be widened by chat):\n"
            f"project_id={rec.project_id or rec.manager_id}\n"
            f"manager_id={rec.manager_id}\n"
            f"repo={rec.repo}\n"  # legacy single-repo prompt compatibility
            f"repos={repos}\n"
            f"status={(project_record.status if project_record else 'active')}\n"
            f"charter={charter or '(ongoing project stewardship)'}\n")
        append = ((base + "\n\n" if base else "") + PROJECT_MANAGER_APPEND
                  + project + handoff)
        session = CoreSession(
            thread_id=thread_id, cwd=home,
            speaker=self.project_manager_speaker(rec.name),
            settings=self.settings, post=self._post, busy=self._busy, idle=self._idle,
            on_session_id=self._sid_saver(
                thread_id, tool_schema_version=ORG_TOOL_SCHEMA_VERSION),
            session_id=resume_id,
            system_append=append,
            extra_tool_namespaces=(self._build_manager_tools(thread_id),),
            final_only=True,
            model_override=model,
            reasoning_effort_override=effort,
        )
        session.on_turn_done = self._on_project_manager_turn_done
        session.on_turn_error = self._on_project_manager_turn_error
        return session

    async def _ensure_orchestrator(self, thread_id: str) -> None:
        if self.store.get(thread_id) is None:
            self.store.put(thread_id, ThreadRecord(
                role="orchestrator", name="orchestrator",
                backend=self.settings.agent_backend))
        self.store.set_orchestrator_thread(thread_id)

    # ---- supervision inbox ----------------------------------------------

    def _note(self, text: str) -> None:
        self._inbox.append(text)
        if len(self._inbox) > MAX_INBOX:
            drop = len(self._inbox) - MAX_INBOX
            self._inbox = self._inbox[-MAX_INBOX:]
            log.warning("inbox exceeded %d notes; dropped %d oldest", MAX_INBOX, drop)
        log.info("inbox note: %s", text[:160])

    def _manager_note(self, manager_thread: str, text: str) -> None:
        inbox = self._manager_inboxes.setdefault(manager_thread, [])
        inbox.append(text)
        if len(inbox) > MAX_INBOX:
            drop = len(inbox) - MAX_INBOX
            del inbox[:drop]
            log.warning(
                "manager inbox %s exceeded %d notes; dropped %d oldest",
                manager_thread, MAX_INBOX, drop)
        log.info("manager inbox note thread=%s: %s", manager_thread, text[:160])

    async def _route_worker_supervision(
        self, rec: ThreadRecord, note: str,
    ) -> None:
        manager = self._find_manager(rec.supervisor_id) if rec.supervisor_id else None
        if manager:
            self._manager_note(manager[0], note)
            await self._wake_project_manager(manager[0])
        else:
            self._note(note)
            await self._wake_orchestrator()

    async def _wake_project_manager(self, manager_thread: str) -> None:
        inbox = self._manager_inboxes.get(manager_thread)
        if not inbox or manager_thread in self._manager_waking:
            return
        rec = self.store.get(manager_thread)
        if rec is None or rec.role != "project_manager" \
                or rec.manager_status == "dismissed":
            # A missing supervisor must never make worker evidence disappear.
            notes = self._manager_inboxes.pop(manager_thread, [])
            for note in notes:
                self._note(f"[manager unavailable] {note}")
            await self._wake_orchestrator()
            return
        self._manager_waking.add(manager_thread)
        try:
            session = await self._ensure_session(manager_thread, rec)
            if session is None:
                notes = self._manager_inboxes.pop(manager_thread, [])
                for note in notes:
                    self._note(f"[manager {rec.manager_id} failed to start] {note}")
                await self._wake_orchestrator()
                return
            while self._manager_inboxes.get(manager_thread):
                await asyncio.sleep(self.WAKE_COALESCE_SECS)
                notes = self._manager_inboxes.pop(manager_thread, [])
                if not notes:
                    break
                snapshot = self._project_snapshot(rec.manager_id)
                digest = (
                    "[project inbox]\n" + "\n".join(f"- {n}" for n in notes)
                    + "\n\n[code-generated project state]\n" + snapshot)
                await session.submit(digest, quiet_ok=True)
        finally:
            self._manager_waking.discard(manager_thread)

    # ---- dashboard (the shared #general status board) -------------------

    def _find_manager(self, manager_id: str) -> tuple[str, ThreadRecord] | None:
        if not manager_id:
            return None
        for thread_id, rec in self.store.managers().items():
            if rec.manager_id == manager_id:
                return thread_id, rec
        return None

    def _find_project(self, project_id: str) -> ProjectRecord | None:
        return self.store.projects().get(project_id)

    def _project_for_manager(self, manager: ThreadRecord) -> ProjectRecord | None:
        project_id = manager.project_id or manager.manager_id
        return self._find_project(project_id)

    def _project_snapshot(self, manager_id: str) -> str:
        found = self._find_manager(manager_id)
        if found is None:
            return "(project manager missing)"
        thread_id, manager = found
        project = self._project_for_manager(manager)
        live = self.sessions.get(thread_id)
        repos = project.repos if project else ([manager.repo] if manager.repo else [])
        lines = [
            f"project={(project.name if project else Path(manager.repo).name)} "
            f"project_id={(project.project_id if project else manager.manager_id)}",
            f"status={(project.status if project else manager.manager_status or 'active')}",
            "repos=" + (", ".join(repos) if repos else "(none)"),
            f"charter={((project.charter if project else manager.task) or '(none)')[:600]}",
            "last_summary=" + (
                ((project.last_summary if project else manager.last_summary)
                 or "(none)")[:1000]),
            f"manager={manager.manager_id} runtime={live.status if live else 'dormant'}",
        ]
        children = [
            (tid, worker) for tid, worker in self.store.workers().items()
            if worker.supervisor_id == manager_id
        ]
        if not children:
            lines.append("workers=(none)")
        for tid, worker in children:
            runtime = self.sessions.get(tid)
            lines.append(
                f"- {worker.worker_id}: status={worker.worker_status or 'working'} "
                f"runtime={runtime.status if runtime else 'dormant'} "
                f"checks={worker.checks or 'not-run'} task={worker.task[:100]}")
        return "\n".join(lines)

    def _organization_snapshot(self) -> dict[str, Any]:
        """Deterministic org truth shared by browser, editor, CLI, and tests."""
        def worker_json(thread_id: str, rec: ThreadRecord) -> dict[str, Any]:
            runtime = self.sessions.get(thread_id)
            status = rec.worker_status or "working"
            if rec.worker_id in self._pending_delivery:
                status = "approve"
            return {
                "id": rec.worker_id,
                "thread_id": thread_id,
                "name": rec.name,
                "role": "worker",
                "project_id": rec.project_id,
                "repo": rec.repo,
                "task": rec.task,
                "status": status,
                "runtime": runtime.status if runtime else "dormant",
                "checks": rec.checks,
            }

        projects: list[dict[str, Any]] = []
        assigned_threads: set[str] = set()
        managers = self.store.managers()
        workers = self.store.workers()
        for project in sorted(
            self.store.projects().values(), key=lambda p: (p.created_at, p.project_id),
        ):
            if project.status == "archived":
                continue
            manager_pair = next(
                ((tid, rec) for tid, rec in managers.items()
                 if rec.project_id == project.project_id
                 or (not rec.project_id and rec.manager_id == project.manager_id)),
                None,
            )
            manager_json: dict[str, Any] | None = None
            manager_id = project.manager_id
            if manager_pair:
                manager_thread, manager = manager_pair
                assigned_threads.add(manager_thread)
                runtime = self.sessions.get(manager_thread)
                manager_id = manager.manager_id
                manager_json = {
                    "id": manager.manager_id,
                    "thread_id": manager_thread,
                    "name": manager.name,
                    "role": "project_manager",
                    "status": manager.manager_status or "active",
                    "runtime": runtime.status if runtime else "dormant",
                    "last_summary": project.last_summary or manager.last_summary,
                }
            children: list[dict[str, Any]] = []
            for thread_id, worker in workers.items():
                if (worker.project_id == project.project_id
                        or (not worker.project_id and manager_id
                            and worker.supervisor_id == manager_id)) \
                        and worker.worker_status not in ("dismissed", "delivered"):
                    assigned_threads.add(thread_id)
                    children.append(worker_json(thread_id, worker))
            projects.append({
                "id": project.project_id,
                "name": project.name,
                "charter": project.charter,
                "status": project.status,
                "repos": list(project.repos),
                "manager": manager_json,
                "workers": children,
                "last_summary": project.last_summary,
            })
        independent = [
            worker_json(thread_id, worker)
            for thread_id, worker in workers.items()
            if thread_id not in assigned_threads
            and worker.worker_status not in ("dismissed", "delivered")
            and not worker.project_id
            and not worker.supervisor_id
        ]
        orchestrator = self.sessions.get(self.main_thread)
        return {
            "version": 1,
            "orchestrator": {
                "thread_id": self.main_thread,
                "name": self.settings.bot_name,
                "role": "orchestrator",
                "status": orchestrator.status if orchestrator else "dormant",
            },
            "projects": projects,
            "independent_workers": independent,
        }

    def _render_dashboard(self) -> str:
        """A deterministic snapshot of the fleet, rendered from the store in code
        (never authored by the LLM), so the board is always exactly true."""
        workers = [r for r in self.store.workers().values()
                   if r.worker_status != "dismissed"]

        def bucket(r: ThreadRecord) -> str:
            if r.worker_id in self._pending_delivery:
                return "approve"
            if r.worker_status == "blocked":
                return "blocked"
            if r.worker_status == "delivered":
                return "delivered"
            if r.worker_status == "done":
                return "review"
            return "running"

        cats: dict[str, list[ThreadRecord]] = {
            "running": [], "review": [], "approve": [], "blocked": [], "delivered": []}
        for r in workers:
            cats[bucket(r)].append(r)

        def line(r: ThreadRecord) -> str:
            return f"  • {r.name} · {Path(r.repo).name} — {r.task[:56]}"

        out = [f"📋 {self.settings.bot_name} — live status",
               f"🟢 {len(cats['running'])} running   🔎 {len(cats['review'])} in review"
               f"   🚦 {len(cats['approve'])} to approve   ⛔ {len(cats['blocked'])} blocked"]
        projects = [p for p in self.store.projects().values()
                    if p.status != "archived"]
        if projects:
            out += ["", "🗂 Projects:"]
            for project in projects[-12:]:
                children = [
                    w for w in workers
                    if w.project_id == project.project_id
                    or (not w.project_id and project.manager_id
                        and w.supervisor_id == project.manager_id)
                ]
                counts = {key: 0 for key in cats}
                for child in children:
                    counts[bucket(child)] += 1
                out.append(
                    f"  • {project.name} · {len(project.repos)} repo(s) — "
                    f"{counts['running']} running, {counts['review']} review, "
                    f"{counts['blocked']} blocked")
                for child in children:
                    state = bucket(child)
                    if state in ("blocked", "review", "approve"):
                        out.append(
                            f"    ↳ {child.name} [{state}] — {child.task[:48]}")
        if cats["approve"]:
            out += ["", "🚦 Awaiting your approval:"] + [
                f"  • {r.name} · {Path(r.repo).name} — /approve {r.worker_id}"
                for r in cats["approve"]]
        if cats["blocked"]:
            out += ["", "⛔ Blocked (need you):"] + [line(r) for r in cats["blocked"]]
        if cats["running"]:
            out += ["", "🟢 Running:"] + [line(r) for r in cats["running"]]
        if cats["review"]:
            out += ["", "🔎 Done, awaiting review:"] + [line(r) for r in cats["review"]]
        if cats["delivered"]:
            out += ["", "✅ Recently delivered:"] + [
                f"  • {r.name} · {Path(r.repo).name}" for r in cats["delivered"][-5:]]
        if not workers:
            out += ["", "idle — nothing running. Send me a goal to get started."]
        return "\n".join(out)

    def render_organization_text(self) -> str:
        """Compact, transport-neutral tree for chat and plain terminals."""
        organization = self._organization_snapshot()
        boss = organization["orchestrator"]
        lines = [f"🧭 {boss['name']} [{boss['status']}]", "│"]
        projects = organization["projects"]
        independent = organization["independent_workers"]
        for index, project in enumerate(projects):
            last = index == len(projects) - 1 and not independent
            branch = "└─" if last else "├─"
            repos = ", ".join(Path(value).name for value in project["repos"])
            lines.append(
                f"{branch} 📦 {project['name']} [{project['status']}] · "
                f"{repos or 'no repo'}")
            stem = "   " if last else "│  "
            manager = project["manager"]
            if manager:
                lines.append(
                    f"{stem}{'└─' if not project['workers'] else '├─'} "
                    f"🗂️ {manager['name']} "
                    f"[{manager['status']}/{manager['runtime']}]")
            for worker_index, worker in enumerate(project["workers"]):
                worker_last = worker_index == len(project["workers"]) - 1
                lines.append(
                    f"{stem}{'└─' if worker_last else '├─'} ⚙️ {worker['name']} "
                    f"[{worker['status']}/{worker['runtime']}]")
        if independent:
            lines.append("└─ Independent work")
            for index, worker in enumerate(independent):
                lines.append(
                    f"   {'└─' if index == len(independent) - 1 else '├─'} "
                    f"⚙️ {worker['name']} [{worker['status']}/{worker['runtime']}]")
        if not projects and not independent:
            lines.append("└─ No active projects or workers")
        return "\n".join(lines)

    async def _refresh_organization(self) -> None:
        """Publish one org projection without forcing a chat dashboard edit."""
        organization = self._organization_snapshot()
        if organization == self._last_organization:
            return
        self._last_organization = organization
        self.store.write_organization(organization)
        org_fn = getattr(self.transport, "update_organization", None)
        if org_fn is not None:
            try:
                await org_fn(organization)
            except Exception:  # noqa: BLE001
                log.exception("organization refresh failed")

    async def _refresh_dashboard(self) -> None:
        """Re-render the board and push it if it changed. A no-op on transports that
        don't support a dashboard (web), and never allowed to break the flow."""
        await self._refresh_organization()
        fn = getattr(self.transport, "update_dashboard", None)
        if fn is None:
            return
        text = self._render_dashboard()
        if text == self._last_dashboard:
            return
        self._last_dashboard = text
        try:
            await fn(text)
        except Exception:  # noqa: BLE001
            log.exception("dashboard refresh failed")

    async def _on_orchestrator_turn_done(
        self, session: CoreSession, result: AgentResult,
    ) -> None:
        ids = session.active_delivery_ids
        if not ids:
            return
        if result.is_error or (result.subtype and result.subtype != "success"):
            log.warning("retaining %d failed boss turn(s) for restart recovery", len(ids))
            return
        self.store.acknowledge_boss_turns(ids)

    async def _on_orchestrator_turn_error(
        self, session: CoreSession, error: BaseException,
    ) -> None:
        ids = session.active_delivery_ids
        if not ids:
            return
        if isinstance(error, OutboundDeliveryError):
            # The backend finished; replaying could duplicate external side effects.
            self.store.acknowledge_boss_turns(ids)
            log.error("boss turn completed but output delivery failed; not replaying: %s",
                      error)
        else:
            log.warning("retaining %d crashed boss turn(s) for restart recovery: %s",
                        len(ids), error)

    async def _on_project_manager_turn_done(
        self, session: CoreSession, result: AgentResult,
    ) -> None:
        rec = self.store.get(session.thread_id)
        if rec is None:
            return
        full = (result.result or "").strip()
        meaningful = bool(full and full.upper() != "NOTHING")
        if meaningful:
            summary = full if len(full) <= 1000 else full[:1000] + "…"
            self.store.update(session.thread_id, last_summary=summary)
            if rec.project_id:
                self.store.update_project(rec.project_id, last_summary=summary)
            project = self._project_for_manager(rec)
            self._note(
                f"project manager {rec.manager_id} "
                f"({project.name if project else Path(rec.repo).name}) report: "
                f"{summary}")
            await self._wake_orchestrator()
        self._schedule_project_manager_retirement(session.thread_id, session)

    async def _on_project_manager_turn_error(
        self, session: CoreSession, error: BaseException,
    ) -> None:
        rec = self.store.get(session.thread_id)
        if rec is None:
            return
        detail = str(error) or error.__class__.__name__
        project = self._project_for_manager(rec)
        self._note(
            f"project manager {rec.manager_id} "
            f"({project.name if project else Path(rec.repo).name}) failed a turn: "
            f"{detail[:400]}. Inspect the project and recover supervision.")
        await self._wake_orchestrator()
        self._schedule_project_manager_retirement(session.thread_id, session)

    def _schedule_project_manager_retirement(
        self, thread_id: str, session: CoreSession,
    ) -> None:
        task = asyncio.create_task(
            self._retire_project_manager_runtime(thread_id, session),
            name=f"retire-project-manager-{thread_id}",
        )
        self._retirement_tasks.add(task)
        task.add_done_callback(self._retirement_tasks.discard)

    async def _retire_project_manager_runtime(
        self, thread_id: str, expected_session: CoreSession | None = None,
    ) -> bool:
        """Hibernate an idle manager while keeping its native session resumable."""
        await asyncio.sleep(0)
        rec = self.store.get(thread_id)
        if rec is None or rec.role != "project_manager":
            return False
        session = self.sessions.get(thread_id)
        if expected_session is not None and session is not expected_session:
            return False
        if session is None or getattr(session, "pending", 0):
            return False
        self.sessions.pop(thread_id, None)
        await session.stop()
        log.info("hibernated project manager %s", rec.manager_id)
        return True

    async def _on_worker_turn_done(self, session: CoreSession, result: AgentResult) -> None:
        rec = self.store.get(session.thread_id)
        if rec is None:
            return
        full = (result.result or "").strip()
        new_status = _parse_worker_status(full)  # last real STATUS line; None = unchanged
        if new_status:
            self.store.update(session.thread_id, worker_status=new_status)
        tail = full if len(full) <= 600 else full[:600] + "…"
        status = "errored" if result.is_error else "finished a turn"
        note = (f"worker {rec.worker_id} ({rec.name}, task: {rec.task[:80]}) "
                f"{status}: {tail or '(no text)'}")
        manager = self._find_manager(rec.supervisor_id) if rec.supervisor_id else None
        if manager:
            self._manager_note(manager[0], note)
        else:
            self._note(note)
        await self._refresh_dashboard()
        if manager:
            await self._wake_project_manager(manager[0])
        else:
            await self._wake_orchestrator()
        if new_status in ("done", "delivered"):
            self._schedule_worker_retirement(session.thread_id, session)

    def _schedule_worker_retirement(
        self, thread_id: str, session: CoreSession,
    ) -> None:
        """Retire after the current CoreSession callback has returned."""
        task = asyncio.create_task(
            self._retire_worker_runtime(thread_id, session),
            name=f"retire-worker-{thread_id}",
        )
        self._retirement_tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._retirement_tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                log.error(
                    "worker runtime retirement failed thread=%s: %s",
                    thread_id, error,
                )

        task.add_done_callback(finished)

    async def _retire_worker_runtime(
        self, thread_id: str, expected_session: CoreSession | None = None,
    ) -> bool:
        """Stop terminal worker runtimes while preserving resumable fleet state.

        Yield once so an on_turn_done caller can leave the session run loop. A
        simultaneous follow-up changes the persisted status back to ``working``;
        that wins the race and keeps the live session.
        """
        await asyncio.sleep(0)
        rec = self.store.get(thread_id)
        if rec is None or rec.role != "worker":
            return False
        if rec.worker_status not in ("done", "delivered"):
            return False
        session = self.sessions.get(thread_id)
        if expected_session is not None and session is not expected_session:
            return False
        if session is not None:
            self.sessions.pop(thread_id, None)
            await session.stop()
        if rec.repo and rec.cwd and Path(rec.cwd) != Path(rec.repo):
            await worktrees.terminate_worker_processes(
                Path(rec.cwd), worker_thread_id=thread_id)
        log.info(
            "retired terminal worker runtime %s status=%s",
            rec.worker_id, rec.worker_status,
        )
        return True

    async def _on_worker_turn_error(self, session: CoreSession, error: BaseException) -> None:
        """A worker's turn CRASHED (no agent result — e.g. the SDK hit a fatal read
        error on an oversized message). The session already tried to reconnect itself;
        wake the orchestrator so it FOLLOWS UP — retries, re-briefs, or tells the boss —
        instead of leaving the worker silently stalled. This is the self-heal path."""
        rec = self.store.get(session.thread_id)
        if rec is None:
            return
        if isinstance(error, OutboundDeliveryError):
            note = (
                f"worker {rec.worker_id} ({rec.name}) completed agent activity but its "
                f"chat output could not be delivered: {error}. Inspect the worktree and "
                "follow up without blindly repeating the turn; its side effects may "
                "already be complete.")
            await self._refresh_dashboard()
            await self._route_worker_supervision(rec, note)
            return
        detail = str(error) or error.__class__.__name__
        if len(detail) > 300:
            detail = detail[:300] + "…"
        recovered = session.status not in ("error", "stopped")
        note = (
            f"worker {rec.worker_id} ({rec.name}) hit an error mid-turn and that turn "
            f"was lost: {detail}. "
            + ("I reconnected the session — decide how to get it moving again (retry, or "
               "re-brief to avoid the trigger, e.g. don't dump huge outputs into the chat), "
               "or tell the boss if it's not worth continuing."
               if recovered else
               "The session could NOT recover — tell the boss it needs a look."))
        await self._refresh_dashboard()
        await self._route_worker_supervision(rec, note)

    # Coalescing window: near-simultaneous worker events (e.g. two workers finish
    # together, or a boss interjection followed by the worker's reply) become one
    # orchestrator turn instead of several.
    WAKE_COALESCE_SECS = 2.0

    async def _wake_orchestrator(self) -> None:
        """Deliver the accumulated inbox to the orchestrator as one digest turn."""
        if self._waking or not self._inbox:
            return
        othread = self.store.orchestrator_thread
        if othread is None:
            return
        rec = self.store.get(othread)
        if rec is None:
            return
        # Claim the wake BEFORE any await, or a second worker finishing during the
        # (suspending) session start passes the guard and double-drains the inbox.
        self._waking = True
        try:
            session = await self._ensure_session(othread, rec)
            if session is None:
                return
            # Loop-drain: a worker finishing while we're mid-digest appends to
            # _inbox; the while-check re-runs with no await between it and the
            # `finally` below, so no completion note can be stranded.
            while self._inbox:
                await asyncio.sleep(self.WAKE_COALESCE_SECS)
                notes, self._inbox = self._inbox, []
                if not notes:
                    break
                digest = "[fleet inbox]\n" + "\n".join(f"- {n}" for n in notes)
                # reply where the boss actually is, not into a silent #general;
                # quiet_ok: a checkpoint needing nothing boss-facing posts no message.
                vision, self._pending_vision = self._pending_vision, []
                items = self._vision_items(vision)
                if items:
                    # hand the orchestrator the worker's latest screenshots as VISION so
                    # it can judge the work with its own eyes, not relay a text summary.
                    await session.submit_media(
                        digest + "\n\n[The worker's latest screenshots are attached AND "
                        "saved in your ./.beaboss-inbox/. SCRUTINISE them for what's WRONG "
                        "— clipping, cut-off edges, artifacts, wrong perspective, muddy or "
                        "ugly work — not just what's right; a visible flaw you approve that "
                        "the boss then catches is a failure. If you report this to the boss, "
                        "SHOW it with chat.send_photo('.beaboss-inbox/<filename>') so they "
                        "SEE it, don't just describe it.]",
                        items, reply_to=self._last_boss_thread, quiet_ok=True)
                else:
                    await session.submit(
                        digest, reply_to=self._last_boss_thread, quiet_ok=True)
        finally:
            self._waking = False

    # ---- fleet tools (the orchestrator's powers) -------------------------

    def _build_fleet_tools(self) -> ToolNamespace:
        engine = self
        specs: list[AgentTool] = []

        def fleet_tool(name: str, description: str, input_schema: dict[str, Any]):
            def decorate(handler):
                specs.append(AgentTool(name, description, input_schema, handler))
                return handler
            return decorate

        def ok(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}]}

        def err(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        @fleet_tool(
            "routing_status",
            "Inspect your own agent-routing configuration and health. Returns the "
            "effective fast/balanced/deep model+effort map and the profiles persisted "
            "for workers. Use this before diagnosing cost, latency, model, or tier issues.",
            {"type": "object", "properties": {}},
        )
        async def routing_status(args: dict[str, Any]) -> dict[str, Any]:
            lines = [_routing_report(engine.settings), "", "persisted workers:"]
            workers = list(engine.store.workers().values())
            if not workers:
                lines.append("- (none)")
            for rec in workers:
                lines.append(
                    f"- {rec.worker_id}: tier={rec.tier or '(legacy/unknown)'}; "
                    f"model={rec.model or '(not recorded)'}; "
                    f"effort={rec.reasoning_effort or '(not recorded)'}; "
                    f"status={rec.worker_status or 'working'}")
            return ok("\n".join(lines))

        @fleet_tool(
            "list_repos",
            "List the repositories available under the projects root, with their "
            "path and a one-line description. inspect_repo one before you brief.",
            {"type": "object", "properties": {}},
        )
        async def list_repos(args: dict[str, Any]) -> dict[str, Any]:
            root = engine.settings.projects_root
            try:
                rows = []
                for p in sorted(root.iterdir()):
                    if not p.is_dir() or p.name.startswith("."):
                        continue
                    if (p / ".git").exists():
                        hint = _repo_hint(p)
                        rows.append(f"- {p.name} — {p}" + (f" · {hint}" if hint else ""))
                    else:
                        rows.append(f"- {p.name} — {p} (not a git repo)")
                return ok("\n".join(rows) or "(no repositories found)")
            except OSError as e:
                return err(f"could not list {root}: {e}")

        @fleet_tool(
            "inspect_repo",
            "Look inside a repo BEFORE you brief a worker or judge their work: its "
            "guide docs (AGENTS.md / CLAUDE.md / README), top-level layout, and the "
            "likely check command. Manage as a technical lead who's read the code — "
            "you can also Read/Grep the repo directly at its path.",
            {"type": "object",
             "properties": {
                 "repo": {"type": "string",
                          "description": "repo name under the projects root, or absolute path"},
             },
             "required": ["repo"]},
        )
        async def inspect_repo(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._inspect_repo(str(args.get("repo", "")))

        @fleet_tool(
            "create_project",
            "Create or reuse an outcome-based project with one hibernating manager. "
            "A project may span multiple repositories; choose the grouping from shared "
            "goals, decisions, dependencies, and delivery timing—not repo count.",
            {"type": "object", "properties": {
                "name": {"type": "string"},
                "charter": {"type": "string"},
                "repos": {"type": "array", "items": {"type": "string"},
                          "minItems": 1},
                "tier": {"type": "string", "enum": ["fast", "balanced", "deep"]},
            }, "required": ["name", "charter", "repos"]},
        )
        async def create_project(args: dict[str, Any]) -> dict[str, Any]:
            raw_repos = args.get("repos")
            repos = [str(value) for value in raw_repos] \
                if isinstance(raw_repos, list) else []
            return await engine._create_project(
                str(args.get("name", "")), str(args.get("charter", "")), repos,
                tier=str(args.get("tier", "")).strip().lower() or None)

        @fleet_tool(
            "update_project_scope",
            "Replace a project's assigned repository set. Only use when the outcome "
            "genuinely crosses or stops involving repositories; code validates scope.",
            {"type": "object", "properties": {
                "project_id": {"type": "string"},
                "repos": {"type": "array", "items": {"type": "string"},
                          "minItems": 1},
            }, "required": ["project_id", "repos"]},
        )
        async def update_project_scope(args: dict[str, Any]) -> dict[str, Any]:
            raw_repos = args.get("repos")
            repos = [str(value) for value in raw_repos] \
                if isinstance(raw_repos, list) else []
            return await engine._update_project_scope(
                str(args.get("project_id", "")), repos)

        @fleet_tool(
            "update_project_status",
            "Set the code-owned project lifecycle after evidence changes. Completed "
            "projects retain their resumable manager. Use dismiss_project_manager "
            "to archive retired work.",
            {"type": "object", "properties": {
                "project_id": {"type": "string"},
                "status": {"type": "string", "enum": [
                    "active", "blocked", "completed"]},
            }, "required": ["project_id", "status"]},
        )
        async def update_project_status(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._update_project_status(
                str(args.get("project_id", "")), str(args.get("status", "")))

        @fleet_tool(
            "message_project",
            "Send a project-level outcome, constraint, or decision by stable project id.",
            {"type": "object", "properties": {
                "project_id": {"type": "string"}, "text": {"type": "string"},
            }, "required": ["project_id", "text"]},
        )
        async def message_project(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._message_project(
                str(args.get("project_id", "")), str(args.get("text", "")))

        @fleet_tool(
            "hire_project_manager",
            "Backward-compatible shortcut for a single-repository project. Prefer "
            "create_project when shaping new work.",
            {"type": "object", "properties": {
                "repo": {"type": "string"},
                "charter": {"type": "string"},
                "tier": {"type": "string", "enum": ["fast", "balanced", "deep"]},
            }, "required": ["repo", "charter"]},
        )
        async def hire_project_manager(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._hire_project_manager(
                str(args.get("repo", "")), str(args.get("charter", "")),
                tier=str(args.get("tier", "")).strip().lower() or None)

        @fleet_tool(
            "message_project_manager",
            "Send a project outcome, constraint, decision, or follow-up to a manager.",
            {"type": "object", "properties": {
                "manager_id": {"type": "string"}, "text": {"type": "string"},
            }, "required": ["manager_id", "text"]},
        )
        async def message_project_manager(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._message_project_manager(
                str(args.get("manager_id", "")), str(args.get("text", "")))

        @fleet_tool(
            "project_status",
            "Code-owned project/manager/worker state. Pass project_id or manager_id.",
            {"type": "object", "properties": {
                "project_id": {"type": "string"},
                "manager_id": {"type": "string"}}},
        )
        async def project_status(args: dict[str, Any]) -> dict[str, Any]:
            want = (str(args.get("project_id", "")).strip()
                    or str(args.get("manager_id", "")).strip())
            managers = engine.store.managers().items()
            blocks = [engine._project_snapshot(rec.manager_id)
                      for _tid, rec in managers
                      if not want or rec.manager_id == want or rec.project_id == want]
            return ok("\n\n".join(blocks) or "(no matching projects)")

        @fleet_tool(
            "dismiss_project_manager",
            "Dismiss an idle project manager. Refuses while owned work is unfinished.",
            {"type": "object", "properties": {
                "manager_id": {"type": "string"}}, "required": ["manager_id"]},
        )
        async def dismiss_project_manager(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._dismiss_project_manager(
                str(args.get("manager_id", "")))

        @fleet_tool(
            "spawn_worker",
            "Hire a worker for one task. Creates a visible thread and an isolated "
            "git worktree of the repo, briefs the worker, and they start working. "
            "The brief must be self-contained (goal, constraints, definition of "
            "done). Returns the worker's id.",
            {"type": "object",
             "properties": {
                 "repo": {"type": "string",
                          "description": "repo name under the projects root, or absolute path"},
                 "task": {"type": "string", "description": "the self-contained brief"},
                 "tier": {"type": "string", "enum": ["fast", "balanced", "deep"],
                          "description": "model tier for this worker: 'fast' (cheap/quick "
                                         "— routine or mechanical work), 'balanced' "
                                         "(default), 'deep' (top model — hard or ambiguous "
                                         "work). Match the model to the difficulty; omit "
                                         "for balanced."},
             },
             "required": ["repo", "task"]},
        )
        async def spawn_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._spawn_worker(
                str(args.get("repo", "")), str(args.get("task", "")),
                tier=str(args.get("tier", "")).strip().lower() or None)

        @fleet_tool(
            "message_worker",
            "Say something to a worker. Your message is posted in their thread "
            "(visible to the boss) and becomes the worker's next input.",
            {"type": "object",
             "properties": {
                 "worker_id": {"type": "string"},
                 "text": {"type": "string"},
             },
             "required": ["worker_id", "text"]},
        )
        async def message_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._message_worker(str(args.get("worker_id", "")),
                                               str(args.get("text", "")))

        @fleet_tool(
            "worker_status",
            "Current fleet state. Pass worker_id for one worker, omit for all.",
            {"type": "object",
             "properties": {"worker_id": {"type": "string"}}},
        )
        async def worker_status(args: dict[str, Any]) -> dict[str, Any]:
            rows = []
            for tid, rec in engine.store.workers().items():
                cid = rec.worker_id
                want = str(args.get("worker_id", "")).strip()
                if want and want != cid:
                    continue
                live = engine.sessions.get(tid)
                state = live.status if live else "dormant"
                profile = (
                    f"{rec.tier or 'legacy'}:{rec.model or 'default'}/"
                    f"{rec.reasoning_effort or 'default'}")
                rows.append(f"- {cid} ({rec.name}) [{state}] repo={rec.repo} "
                            f"status={rec.worker_status or 'working'} profile={profile} "
                            f"task={rec.task[:100]}")
            return ok("\n".join(rows) or "(no workers)")

        @fleet_tool(
            "dismiss_worker",
            "End a worker's engagement and remove its clean worktree. Refuses a "
            "dirty worktree so uncommitted work remains active and visible.",
            {"type": "object",
             "properties": {"worker_id": {"type": "string"}},
             "required": ["worker_id"]},
        )
        async def dismiss_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._dismiss_worker(str(args.get("worker_id", "")))

        @fleet_tool(
            "review_worker",
            "Inspect a worker's committed work before delivery: returns the diff of "
            "their branch vs your checkout, whether everything is committed, and which "
            "delivery routes are available (a local 'merge' always; 'pr' if a remote + "
            "authenticated gh exist). Read-only — use it to surface the change to the boss.",
            {"type": "object",
             "properties": {"worker_id": {"type": "string"}},
             "required": ["worker_id"]},
        )
        async def review_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._review_worker(str(args.get("worker_id", "")))

        @fleet_tool(
            "run_checks",
            "Run a check command (tests / build / lint) INSIDE a worker's worktree "
            "and get back the REAL exit code + output — the way to VERIFY work "
            "actually passes instead of trusting the worker's word. e.g. "
            "command='uv run pytest' or 'npm test'. Do this before requesting delivery.",
            {"type": "object",
             "properties": {
                 "worker_id": {"type": "string"},
                 "command": {"type": "string",
                             "description": "the check command, e.g. 'uv run pytest'"},
             },
             "required": ["worker_id", "command"]},
        )
        async def run_checks(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._run_checks(str(args.get("worker_id", "")),
                                            str(args.get("command", "")))

        delivery_behavior = (
            "records a request bound to the current commit; the boss's /approve "
            "lands that exact revision"
            if engine.settings.deploy_braveness == "conservative"
            else "lands immediately; call it only after the boss has clearly approved"
        )

        @fleet_tool(
            "deliver_worker",
            "Deliver a worker's finished work after review. In this deployment it "
            f"{delivery_behavior}. method='merge' merges into the repository's "
            "resolved default branch and pushes it when a remote exists; method='pr' "
            "pushes and opens a GitHub PR. Refuses dirty, empty, or failed-check work.",
            {"type": "object",
             "properties": {
                 "worker_id": {"type": "string"},
                 "method": {"type": "string", "enum": ["merge", "pr"]},
             },
             "required": ["worker_id", "method"]},
        )
        async def deliver_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._deliver_worker(str(args.get("worker_id", "")),
                                                str(args.get("method", "")))

        return ToolNamespace(
            name="fleet",
            description="Manage repositories, workers, verification, and delivery.",
            tools=tuple(specs),
        )

    def _build_manager_tools(self, manager_thread: str) -> ToolNamespace:
        """A deliberately small, project-scoped management surface."""
        engine = self
        specs: list[AgentTool] = []

        def project_tool(name: str, description: str, schema: dict[str, Any]):
            def decorate(handler):
                specs.append(AgentTool(name, description, schema, handler))
                return handler
            return decorate

        def err(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        def manager() -> ThreadRecord | None:
            rec = engine.store.get(manager_thread)
            return rec if rec and rec.role == "project_manager" else None

        def project(rec: ThreadRecord) -> ProjectRecord | None:
            return engine._project_for_manager(rec)

        @project_tool(
            "inspect_project",
            "Inspect every repository assigned to this project's durable scope.",
            {"type": "object", "properties": {}},
        )
        async def inspect_project(args: dict[str, Any]) -> dict[str, Any]:
            rec = manager()
            if rec is None:
                return err("manager missing")
            assigned = project(rec)
            repos = assigned.repos if assigned else [rec.repo]
            bodies: list[str] = []
            for repo in repos:
                result = await engine._inspect_repo(repo)
                if result.get("is_error"):
                    return result
                bodies.append(result["content"][0]["text"])
            return {"content": [{"type": "text", "text": "\n\n".join(bodies)}]}

        @project_tool(
            "spawn_worker",
            "Hire a worker in one repository assigned to this project. Specify repo "
            "when the project spans more than one.",
            {"type": "object", "properties": {
                "task": {"type": "string"},
                "repo": {"type": "string"},
                "tier": {"type": "string", "enum": ["fast", "balanced", "deep"]},
            }, "required": ["task"]},
        )
        async def spawn_worker(args: dict[str, Any]) -> dict[str, Any]:
            rec = manager()
            if rec is None:
                return err("manager missing")
            assigned = project(rec)
            if assigned and assigned.status in ("completed", "archived"):
                return err(
                    f"project {assigned.project_id} is {assigned.status}; the global "
                    "orchestrator must reactivate it before new work")
            repos = assigned.repos if assigned else [rec.repo]
            requested = str(args.get("repo", "")).strip()
            if requested:
                resolved = engine._resolve_repo(requested)
                if resolved is None or str(resolved) not in repos:
                    return err("repository is outside this project's assigned scope")
                repo = str(resolved)
            elif len(repos) == 1:
                repo = repos[0]
            else:
                return err("repo is required for a multi-repository project")
            return await engine._spawn_worker(
                repo, str(args.get("task", "")),
                tier=str(args.get("tier", "")).strip().lower() or None,
                supervisor_id=rec.manager_id,
                project_id=(assigned.project_id if assigned else rec.project_id))

        @project_tool(
            "message_worker",
            "Steer one of your own workers; workers owned by other projects are refused.",
            {"type": "object", "properties": {
                "worker_id": {"type": "string"}, "text": {"type": "string"},
            }, "required": ["worker_id", "text"]},
        )
        async def message_worker(args: dict[str, Any]) -> dict[str, Any]:
            rec = manager()
            if rec is None:
                return err("manager missing")
            assigned = project(rec)
            if assigned and assigned.status in ("completed", "archived"):
                return err(
                    f"project {assigned.project_id} is {assigned.status}; the global "
                    "orchestrator must reactivate it before new work")
            return await engine._message_worker(
                str(args.get("worker_id", "")), str(args.get("text", "")),
                supervisor_id=rec.manager_id)

        @project_tool(
            "worker_status",
            "Show code-owned state for workers in your project only.",
            {"type": "object", "properties": {}},
        )
        async def worker_status(args: dict[str, Any]) -> dict[str, Any]:
            rec = manager()
            if rec is None:
                return err("manager missing")
            return {"content": [{"type": "text",
                                 "text": engine._project_snapshot(rec.manager_id)}]}

        @project_tool(
            "dismiss_worker",
            "Dismiss one of your own workers; refuses dirty workspaces.",
            {"type": "object", "properties": {
                "worker_id": {"type": "string"}}, "required": ["worker_id"]},
        )
        async def dismiss_worker(args: dict[str, Any]) -> dict[str, Any]:
            rec = manager()
            if rec is None:
                return err("manager missing")
            return await engine._dismiss_worker(
                str(args.get("worker_id", "")), supervisor_id=rec.manager_id)

        @project_tool(
            "review_worker",
            "Inspect committed diff and delivery routes for one of your own workers.",
            {"type": "object", "properties": {
                "worker_id": {"type": "string"}}, "required": ["worker_id"]},
        )
        async def review_worker(args: dict[str, Any]) -> dict[str, Any]:
            rec = manager()
            if rec is None:
                return err("manager missing")
            return await engine._review_worker(
                str(args.get("worker_id", "")), supervisor_id=rec.manager_id)

        @project_tool(
            "run_checks",
            "Run a bounded check command inside one of your own worker worktrees.",
            {"type": "object", "properties": {
                "worker_id": {"type": "string"}, "command": {"type": "string"},
            }, "required": ["worker_id", "command"]},
        )
        async def run_checks(args: dict[str, Any]) -> dict[str, Any]:
            rec = manager()
            if rec is None:
                return err("manager missing")
            return await engine._run_checks(
                str(args.get("worker_id", "")), str(args.get("command", "")),
                supervisor_id=rec.manager_id)

        return ToolNamespace(
            name="project",
            description=("Manage workers and verification inside one outcome project "
                         "and its assigned repositories. Delivery remains with the "
                         "global orchestrator."),
            tools=tuple(specs),
        )

    # ---- fleet operations ------------------------------------------------

    def _find_worker(
        self, worker_id: str, supervisor_id: str | None = None,
    ) -> tuple[str, ThreadRecord] | None:
        for tid, rec in self.store.workers().items():
            if (rec.worker_id == worker_id
                    and (supervisor_id is None
                         or rec.supervisor_id == supervisor_id)):
                return tid, rec
        return None

    def _resolve_repo(self, repo_raw: str) -> Path | None:
        """A repo name or path → a real dir, CONSTRAINED to the projects root. The
        orchestrator ingests untrusted text (repo docs via inspect_repo, worker
        diffs via review_worker), so it is itself an injection surface — an absolute
        path must never let it root a bypassPermissions worker over host creds or
        other mounts outside the projects root."""
        if not repo_raw.strip():
            return None
        repo = Path(repo_raw)
        if not repo.is_absolute():
            repo = self.settings.projects_root / repo_raw
        repo = repo.resolve()
        try:
            repo.relative_to(self.settings.projects_root.resolve())
        except ValueError:
            return None  # outside the projects root — refused
        return repo if repo.is_dir() else None

    async def _create_project(
        self, name: str, charter: str, repos_raw: list[str],
        tier: str | None = None,
    ) -> dict[str, Any]:
        def err(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        if not name.strip() or not charter.strip() or not repos_raw:
            return err("name, charter, and at least one repository are required")
        if tier and tier not in ("fast", "balanced", "deep"):
            return err(f"unknown tier '{tier}' — use fast, balanced, or deep")
        repos: list[Path] = []
        for raw in repos_raw:
            repo = self._resolve_repo(str(raw))
            if repo is None:
                return err(f"no such repo: {raw}")
            if repo not in repos:
                repos.append(repo)
        canonical = [str(repo) for repo in repos]

        for project in self.store.projects().values():
            if (project.status != "archived"
                    and project.name.casefold() == name.strip().casefold()
                    and set(project.repos) == set(canonical)):
                self.store.update_project(
                    project.project_id, charter=charter.strip(), status="active")
                found = self._find_manager(project.manager_id)
                if found:
                    self.store.update(found[0], task=charter.strip())
                result = await self._message_project(
                    project.project_id, charter.strip())
                if result.get("is_error"):
                    return result
                self._action(f"reuse_project({project.project_id})")
                return {"content": [{"type": "text", "text":
                        f"reused project manager for project {project.project_id}; "
                        "the new charter was delivered."}]}

        selected_tier, model, effort = self.settings.resolve_worker_profile(
            tier or "balanced")
        base_id = worker_id_for(name.strip()) or "project"
        taken = set(self.store.projects()) | {
            r.manager_id for r in self.store.managers().values()}
        project_id = base_id
        suffix = 2
        while project_id in taken:
            project_id = f"{base_id}-{suffix}"
            suffix += 1
        manager_id = project_id
        manager_name = f"{name.strip()} PM"
        home = self.settings.state_dir / "managers" / f"{manager_id}-home"
        home.mkdir(parents=True, exist_ok=True)

        assert self.transport is not None
        thread_id = await self.transport.create_thread(
            f"{PROJECT_MANAGER_EMOJI} {name.strip()} · project manager")
        rec = ThreadRecord(
            role="project_manager", name=manager_name, cwd=str(home),
            repo=canonical[0],
            task=charter.strip(), manager_id=manager_id, manager_status="active",
            project_id=project_id,
            tier=selected_tier, model=model or "", reasoning_effort=effort or "",
            backend=self.settings.agent_backend,
            models=({self.settings.agent_backend: model} if model else {}),
            reasoning_efforts=(
                {self.settings.agent_backend: effort} if effort else {}),
        )
        project = ProjectRecord(
            project_id=project_id, name=name.strip(), charter=charter.strip(),
            repos=canonical, status="active", manager_id=manager_id,
            manager_thread=thread_id,
        )
        self.store.put_project(project)
        self.store.put(thread_id, rec)
        update_thread = getattr(self.transport, "update_thread", None)
        if update_thread is not None:
            await update_thread(
                thread_id, role="project_manager", repo=canonical[0],
                project_id=project_id, manager_id=manager_id,
                supervisor_id="", status="active")
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM,
            text=(f"Project room opened for {name.strip()} across "
                  f"{len(canonical)} repository scope(s). {manager_name} retains "
                  "project context and hibernates when idle.")))
        await self._post(Outbound(
            thread_id=thread_id, speaker=self.orchestrator_speaker(),
            text=charter.strip()))
        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return err("project manager session failed to start (see project room)")
        await session.submit(
            "[Initial charter from the global orchestrator]\n" + charter.strip())
        await self._refresh_dashboard()
        self._action(f"create_project → {project_id} · {name.strip()}")
        return {"content": [{"type": "text", "text":
                f"created project {project_id} with manager {manager_id} across "
                f"{len(canonical)} repo(s); project room and charter are live."}]}

    async def _hire_project_manager(
        self, repo_raw: str, charter: str, tier: str | None = None,
    ) -> dict[str, Any]:
        """Backward-compatible single-repository project creation tool."""
        repo = self._resolve_repo(repo_raw)
        if repo is None:
            return {"content": [{"type": "text", "text":
                    f"no such repo: {repo_raw}"}], "is_error": True}
        return await self._create_project(repo.name, charter, [str(repo)], tier)

    async def _message_project(
        self, project_id: str, text: str,
    ) -> dict[str, Any]:
        project = self._find_project(project_id.strip())
        if project is None:
            return {"content": [{"type": "text", "text":
                    f"no such project: {project_id}"}], "is_error": True}
        return await self._message_project_manager(project.manager_id, text)

    async def _update_project_scope(
        self, project_id: str, repos_raw: list[str],
    ) -> dict[str, Any]:
        project = self._find_project(project_id.strip())
        if project is None:
            return {"content": [{"type": "text", "text":
                    f"no such project: {project_id}"}], "is_error": True}
        if not repos_raw:
            return {"content": [{"type": "text", "text":
                    "at least one repository is required"}], "is_error": True}
        if project.status in ("completed", "archived"):
            return {"content": [{"type": "text", "text":
                    f"project {project.project_id} is {project.status}; reactivate it "
                    "before changing scope"}], "is_error": True}
        repos: list[str] = []
        for raw in repos_raw:
            repo = self._resolve_repo(str(raw))
            if repo is None:
                return {"content": [{"type": "text", "text":
                        f"no such repo: {raw}"}], "is_error": True}
            if str(repo) not in repos:
                repos.append(str(repo))
        removed = set(project.repos) - set(repos)
        live_removed = [
            worker.worker_id for worker in self.store.workers().values()
            if (worker.project_id == project.project_id
                or worker.supervisor_id == project.manager_id)
            and worker.repo in removed
            and worker.worker_status not in ("dismissed", "delivered")
        ]
        if live_removed:
            return {"content": [{"type": "text", "text":
                    "refused to remove repository scope while workers remain active: "
                    + ", ".join(live_removed)}], "is_error": True}
        found = self._find_manager(project.manager_id)
        if found is None:
            return {"content": [{"type": "text", "text":
                    f"project manager missing for {project.project_id}"}], "is_error": True}
        thread_id, manager = found
        live = self.sessions.get(thread_id)
        if live is not None and (
                live.status in ("busy", "waiting") or live.pending > 0):
            return {"content": [{"type": "text", "text":
                    f"project manager {manager.manager_id} is handling work; retry "
                    "the scope change after the turn and queued messages finish"}],
                    "is_error": True}
        old_repos = list(project.repos)
        old_primary = manager.repo
        self.store.update_project(project.project_id, repos=repos)
        self.store.update(thread_id, repo=repos[0])
        live = self.sessions.pop(thread_id, None)
        if live is not None:
            await live.stop()
        update_thread = getattr(self.transport, "update_thread", None)
        if update_thread is not None:
            await update_thread(
                thread_id, repo=repos[0], project_id=project.project_id)
        notice = await self._message_project_manager(
            manager.manager_id,
            "Project scope changed. Re-read the code-generated repository list "
            "before delegating further.")
        if notice.get("is_error"):
            self.store.update_project(project.project_id, repos=old_repos)
            self.store.update(thread_id, repo=old_primary)
            if update_thread is not None:
                await update_thread(
                    thread_id, repo=old_primary, project_id=project.project_id)
            return {"content": [{"type": "text", "text":
                    "project scope change rolled back: "
                    + notice["content"][0]["text"]}], "is_error": True}
        await self._refresh_dashboard()
        self._action(f"update_project_scope({project.project_id})")
        return {"content": [{"type": "text", "text":
                f"project {project.project_id} now spans {len(repos)} repo(s)"}]}

    async def _update_project_status(
        self, project_id: str, status: str,
    ) -> dict[str, Any]:
        project = self._find_project(project_id.strip())
        allowed = ("active", "blocked", "completed", "archived")
        if project is None:
            return {"content": [{"type": "text", "text":
                    f"no such project: {project_id}"}], "is_error": True}
        if status not in allowed:
            return {"content": [{"type": "text", "text":
                    f"invalid status: {status}"}], "is_error": True}
        if project.status == "archived":
            return {"content": [{"type": "text", "text":
                    "archived projects are retired; create a new project instead"}],
                    "is_error": True}
        if status in ("completed", "archived"):
            unfinished = [
                worker.worker_id for worker in self.store.workers().values()
                if (worker.project_id == project.project_id
                    or worker.supervisor_id == project.manager_id)
                and worker.worker_status not in ("dismissed", "delivered")
            ]
            if unfinished:
                return {"content": [{"type": "text", "text":
                        f"refused to mark project {status} with unfinished workers: "
                        + ", ".join(unfinished)}], "is_error": True}
        if status == "archived":
            return {"content": [{"type": "text", "text":
                    "use dismiss_project_manager to archive a retired project"}],
                    "is_error": True}
        self.store.update_project(project.project_id, status=status)
        await self._refresh_dashboard()
        self._action(f"update_project_status({project.project_id}, {status})")
        return {"content": [{"type": "text", "text":
                f"project {project.project_id} is now {status}"}]}

    async def _message_project_manager(
        self, manager_id: str, text: str,
    ) -> dict[str, Any]:
        def err(detail: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": detail}], "is_error": True}

        found = self._find_manager(manager_id.strip())
        if found is None:
            return err(f"no such project manager: {manager_id}")
        if not text.strip():
            return err("text is required")
        thread_id, rec = found
        if rec.manager_status == "dismissed":
            return err(f"project manager {manager_id} is dismissed")
        project = self._project_for_manager(rec)
        if project and project.status in ("completed", "archived"):
            return err(
                f"project {project.project_id} is {project.status}; reactivate it "
                "before sending new work")
        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return err("project manager session unavailable")
        await self._post(Outbound(
            thread_id=thread_id, speaker=self.orchestrator_speaker(),
            text=text.strip()))
        await session.submit(f"[From the global orchestrator]: {text.strip()}")
        self._action(f"message_project_manager({rec.manager_id})")
        return {"content": [{"type": "text", "text": "delivered"}]}

    async def _dismiss_project_manager(self, manager_id: str) -> dict[str, Any]:
        def err(detail: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": detail}], "is_error": True}

        found = self._find_manager(manager_id.strip())
        if found is None:
            return err(f"no such project manager: {manager_id}")
        thread_id, rec = found
        unfinished = [
            worker for worker in self.store.workers().values()
            if worker.supervisor_id == rec.manager_id
            and worker.worker_status not in ("dismissed", "delivered")
        ]
        if unfinished:
            ids = ", ".join(worker.worker_id for worker in unfinished)
            return err(
                f"refused to dismiss {manager_id}: unfinished owned workers: {ids}")
        session = self.sessions.pop(thread_id, None)
        if session is not None:
            await session.stop()
        self.store.update(thread_id, manager_status="dismissed")
        if rec.project_id:
            self.store.update_project(rec.project_id, status="archived")
        update_thread = getattr(self.transport, "update_thread", None)
        if update_thread is not None:
            await update_thread(thread_id, status="dismissed")
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM,
            text=f"{rec.name} dismissed by the orchestrator."))
        if self.transport is not None:
            try:
                await self.transport.close_thread(thread_id)
            except Exception:  # noqa: BLE001
                pass
        await self._refresh_dashboard()
        self._action(f"dismiss_project_manager({manager_id})")
        return {"content": [{"type": "text", "text": f"dismissed {manager_id}"}]}

    async def _inspect_repo(self, repo_raw: str) -> dict[str, Any]:
        """Ground the orchestrator in a repo: its guide docs, layout, check command —
        so it briefs and reviews from real knowledge of the code, not from the outside."""
        repo = self._resolve_repo(repo_raw)
        if repo is None:
            return {"content": [{"type": "text",
                    "text": f"no such repo: {repo_raw}"}], "is_error": True}
        parts = [f"# {repo.name} — {repo}"]
        default = await worktrees.default_branch(repo)
        if default:
            parts.append(f"\n## Default (prod) branch\n`{default}` — this is what ships; "
                         f"land work here to reach prod, NOT a guessed 'main'/'master'.")
        for doc in ("AGENTS.md", "CLAUDE.md", "README.md"):
            text = _read_doc(repo / doc, limit=1800)
            if text:
                parts.append(f"\n## {doc}\n{text}")
        parts.append("\n## Top-level layout\n" + _top_level(repo))
        hint = _test_hint(repo)
        if hint:
            parts.append(f"\n## Likely check command\n`{hint}` — verify by actually "
                         f"running it with run_checks once a worker's on it.")
        body = "\n".join(parts)
        if len(body) > 6000:
            body = body[:6000] + "\n…(truncated — Read/Grep the repo directly for more)"
        return {"content": [{"type": "text", "text": body}]}

    async def _spawn_worker(self, repo_raw: str, task: str,
                            tier: str | None = None,
                            supervisor_id: str = "",
                            project_id: str = "") -> dict[str, Any]:
        def err(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        if not repo_raw.strip() or not task.strip():
            return err("repo and task are both required")
        if tier and tier not in ("fast", "balanced", "deep"):
            return err(f"unknown tier '{tier}' — use fast, balanced, or deep")
        repo = self._resolve_repo(repo_raw)
        if repo is None:
            return err(f"no such repo: {repo_raw}")
        selected_tier, model, effort = self.settings.resolve_worker_profile(tier)

        taken = {r.worker_id for r in self.store.workers().values()}
        taken |= {r.name.lower() for r in self.store.workers().values()}
        name = pick_name(taken, self.settings.worker_names)
        worker_id = worker_id_for(name)

        # Workers fork the repository's default branch, not whichever branch happens
        # to be checked out when the tool call arrives.
        base_branch = ""
        try:
            if await worktrees.is_git_repo(repo):
                base_branch = await worktrees.default_branch(repo) or ""
                if not base_branch:
                    return err(f"can't determine the default branch for {repo.name}")
                worktrees_dir = self.settings.state_dir / "worktrees"
                while (
                    await worktrees.branch_exists(repo, f"worker/{worker_id}")
                    or (worktrees_dir / worker_id).exists()
                ):
                    # A reset deliberately preserves old branches. Give new work a
                    # fresh identity instead of attaching it to an earlier task.
                    taken.add(worker_id)
                    name = pick_name(taken, self.settings.worker_names)
                    worker_id = worker_id_for(name)
                wt = await worktrees.create_worktree(
                    repo, worktrees_dir, worker_id, base_branch=base_branch)
                cwd, isolated = wt, True
            else:
                cwd, isolated = repo, False
        except worktrees.WorktreeError as e:
            return err(f"couldn't set up an isolated workspace for {repo.name}: {e}")

        assert self.transport is not None
        prefix = "↳ " if supervisor_id else ""
        thread_id = await self.transport.create_thread(
            f"{prefix}{WORKER_EMOJI} {name} · {repo.name}")

        rec = ThreadRecord(
            role="worker", name=name, cwd=str(cwd), worker_id=worker_id,
            repo=str(repo), base_branch=base_branch,
            task=task.strip(), worker_status="working", tier=selected_tier,
            model=model or "", reasoning_effort=effort or "",
            backend=self.settings.agent_backend,
            models=({self.settings.agent_backend: model} if model else {}),
            reasoning_efforts=(
                {self.settings.agent_backend: effort} if effort else {}),
            supervisor_id=supervisor_id,
            project_id=project_id,
        )
        self.store.put(thread_id, rec)
        update_thread = getattr(self.transport, "update_thread", None)
        if update_thread is not None:
            await update_thread(
                thread_id, role="worker", repo=str(repo),
                project_id=project_id, supervisor_id=supervisor_id,
                status="working")

        iso_note = ("its own isolated copy · branch worker/" + worker_id if isolated
                    else "⚠️ not a git repo — working directly in the project dir")
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM,
            text=f"{name} hired for {repo.name} ({iso_note}).",
        ))
        # the orchestrator's brief, visible in the thread:
        supervisor = self._find_manager(supervisor_id) if supervisor_id else None
        speaker = (self.project_manager_speaker(supervisor[1].name)
                   if supervisor else self.orchestrator_speaker())
        await self._post(Outbound(
            thread_id=thread_id, speaker=speaker, text=task.strip(),
        ))

        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return err("worker session failed to start (see thread)")
        source = (f"project manager {supervisor[1].name}"
                  if supervisor else "the orchestrator")
        await session.submit(f"[Brief from {source}]\n{task.strip()}")
        await self._refresh_dashboard()
        profile = f"{selected_tier} → {model or 'default'}/{effort or 'default'}"
        if not supervisor_id:
            self._action(f"spawn_worker → {name} · {repo.name} [{profile}]")
        return {"content": [{"type": "text", "text":
                f"spawned worker {worker_id} ({name}) in {iso_note}; "
                f"profile {profile}; thread created. They will report back to "
                f"{'their project manager' if supervisor else 'the fleet inbox'}."}]}

    async def _message_worker(
        self, worker_id: str, text: str, supervisor_id: str | None = None,
    ) -> dict[str, Any]:
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_worker(worker_id.strip(), supervisor_id)
        if found is None:
            return err(f"no such worker: {worker_id}")
        if not text.strip():
            return err("text is required")
        thread_id, rec = found
        # being sent back to work un-sticks a done/blocked marker
        if rec.worker_status in ("done", "blocked"):
            self.store.update(thread_id, worker_status="working")
        manager = self._find_manager(supervisor_id or "")
        speaker = (self.project_manager_speaker(manager[1].name)
                   if manager else self.orchestrator_speaker())
        await self._post(Outbound(
            thread_id=thread_id, speaker=speaker, text=text.strip(),
        ))
        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return err("worker session unavailable")
        source = "your project manager" if manager else "the orchestrator"
        await session.submit(f"[From {source}]: {text.strip()}")
        if supervisor_id is None:
            self._action(f"message_worker({rec.worker_id})")
        return {"content": [{"type": "text", "text": "delivered"}]}

    async def _dismiss_worker(
        self, worker_id: str, supervisor_id: str | None = None,
    ) -> dict[str, Any]:
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_worker(worker_id.strip(), supervisor_id)
        if found is None:
            return err(f"no such worker: {worker_id}")
        thread_id, rec = found

        wt = Path(rec.cwd)
        has_worktree = bool(rec.repo) and wt != Path(rec.repo)
        if has_worktree and wt.exists() and not await worktrees.is_clean(wt):
            return err(
                f"refused to dismiss {rec.name}: their workspace has uncommitted "
                f"changes at {wt}. Have them commit the work, or explicitly discard "
                f"it outside this action.")

        session = self.sessions.pop(thread_id, None)
        if session is not None:
            await session.stop()

        detail = ""
        if has_worktree:
            removed, detail = await worktrees.remove_worktree(
                Path(rec.repo), wt, worker_thread_id=thread_id)
            if not removed:
                return err(f"couldn't dismiss {rec.name}: {detail}")

        self.store.update(thread_id, worker_status="dismissed")
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM,
            text=f"{rec.name} dismissed by the orchestrator.",
        ))
        if self.transport is not None:
            try:
                await self.transport.close_thread(thread_id)
            except Exception:  # noqa: BLE001
                pass
        await self._refresh_dashboard()
        if supervisor_id is None:
            self._action(f"dismiss_worker({worker_id})")
        return {"content": [{"type": "text", "text": f"dismissed {worker_id}"}]}

    async def _review_worker(
        self, worker_id: str, supervisor_id: str | None = None,
    ) -> dict[str, Any]:
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_worker(worker_id.strip(), supervisor_id)
        if found is None:
            return err(f"no such worker: {worker_id}")
        _tid, rec = found
        if not rec.repo or rec.cwd == rec.repo:
            return err(f"{rec.name} isn't working in a git worktree — nothing to review or deliver")
        if not Path(rec.cwd).is_dir():
            return err(f"{rec.name}'s workspace is gone (was it dismissed?) — its work, "
                       f"if any, is on branch worker/{rec.worker_id}")
        repo = Path(rec.repo)
        branch = f"worker/{rec.worker_id}"
        base = rec.base_branch or await worktrees.current_branch(repo)
        if not base:
            return err(f"can't determine {rec.name}'s base branch (detached HEAD) — "
                       f"check out a branch in {repo.name}")
        committed = await worktrees.is_clean(Path(rec.cwd))
        has_work = await worktrees.branch_ahead(repo, base, branch)
        tip = await worktrees.head_sha(Path(rec.cwd))
        base_tip = await worktrees.ref_sha(repo, base)
        merged_local = (
            bool(tip and base_tip and tip != base_tip)
            and await worktrees.branch_merged(repo, base, branch)
        )
        diff = await worktrees.branch_diff(repo, base, branch)
        routes = ["merge"]
        if await worktrees.has_remote(repo) and await worktrees.gh_available():
            routes.insert(0, "pr")
        commit_note = ("all changes committed" if committed else
                       "⚠️ uncommitted changes remain — have the worker commit first")
        if has_work:
            work_note = ""
        elif merged_local:
            work_note = "\n- branch is merged locally; remote delivery may still need retrying"
        else:
            work_note = "\n- ⚠️ nothing committed on the branch yet"
        checks_line = {
            "pass": "✅ passed" if rec.checks_sha and rec.checks_sha == await worktrees.head_sha(Path(rec.cwd))
                    else "passed earlier, but the branch moved since (stale — re-run)",
            "fail": "❌ FAILED — must be fixed before this can be delivered",
        }.get(rec.checks, "not run yet — use run_checks to verify before delivering")
        return {"content": [{"type": "text", "text":
                f"Review of {rec.name} — branch {branch} vs {base}:\n"
                f"- {commit_note}\n"
                f"- checks: {checks_line}\n"
                f"- delivery routes available: {', '.join(routes)}{work_note}\n\n{diff}"}]}

    async def _run_checks(
        self, worker_id: str, command: str,
        supervisor_id: str | None = None,
    ) -> dict[str, Any]:
        """Actually RUN a check command in the worker's worktree — real exit code,
        not the worker's word. Records the verdict + the revision it ran against so
        delivery can gate on it (and notice if the branch moved since)."""
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_worker(worker_id.strip(), supervisor_id)
        if found is None:
            return err(f"no such worker: {worker_id}")
        thread_id, rec = found
        command = command.strip()
        if not command:
            return err("a check command is required (e.g. 'uv run pytest' or 'npm test')")
        cwd = Path(rec.cwd)
        if not cwd.is_dir():
            return err(f"{rec.name}'s workspace is gone — nothing to check")
        code, output = await worktrees.run_command(cwd, command)
        sha = await worktrees.head_sha(cwd)  # the branch tip these checks ran against
        self.store.update(thread_id,
                          checks=("pass" if code == 0 else "fail"), checks_sha=sha)
        verdict = "✅ passed" if code == 0 else f"❌ FAILED (exit {code})"
        if supervisor_id is None:
            self._action(f"run_checks({rec.worker_id}): {'✅' if code == 0 else '❌'}")
        # Surface the REAL result into the worker's own thread so the boss sees proof.
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM,
            text=f"🧪 checks — `{command}` — {verdict}"))
        return {"content": [{"type": "text", "text":
                f"checks for {rec.name} — `{command}` — {verdict}\n\n{output}"}]}

    async def _delivery_preflight(
        self, worker_id: str, method: str, expected_sha: str | None = None,
        expected_base_sha: str | None = None,
    ) -> tuple[DeliveryPlan | None, str | None]:
        """Re-observe the facts delivery depends on immediately before it runs."""
        found = self._find_worker(worker_id.strip())
        if found is None:
            return None, f"no such worker: {worker_id}"
        thread_id, rec = found
        if method not in ("merge", "pr"):
            return None, "method must be 'merge' or 'pr'"
        if rec.worker_status == "delivered":
            return None, f"{rec.name}'s work was already delivered"
        if not rec.repo or rec.cwd == rec.repo:
            return None, f"{rec.name} isn't in a git worktree — nothing to deliver"

        worktree = Path(rec.cwd)
        if not worktree.is_dir():
            return None, f"{rec.name}'s workspace is gone — nothing to deliver"
        if not await worktrees.is_clean(worktree):
            return None, f"{rec.name} has uncommitted changes — have them commit first"

        repo = Path(rec.repo)
        base = rec.base_branch or await worktrees.default_branch(repo)
        if not base:
            return None, f"can't determine {rec.name}'s base branch"
        branch = f"worker/{rec.worker_id}"
        tip = await worktrees.head_sha(worktree)
        base_tip = await worktrees.ref_sha(repo, base)
        ahead = await worktrees.branch_ahead(repo, base, branch)
        already_merged = (
            method == "merge"
            and bool(tip and base_tip and tip != base_tip)
            and await worktrees.branch_merged(repo, base, branch)
        )
        if not ahead and not already_merged:
            return None, f"{rec.name} hasn't committed anything to deliver"

        if expected_sha and tip != expected_sha:
            return None, (
                f"{rec.name}'s branch changed after delivery was requested "
                f"({expected_sha[:8]} → {tip[:8]}). Review the new revision and "
                f"request delivery again.")
        if expected_base_sha and base_tip != expected_base_sha:
            return None, (
                f"the target branch changed after delivery was requested "
                f"({expected_base_sha[:8]} → {base_tip[:8]}). Review against the "
                f"new base and request delivery again.")
        if rec.checks == "fail":
            return None, (
                f"{rec.name}'s checks last FAILED — have them fix it and re-run "
                f"run_checks until it's green before delivery.")
        if rec.checks == "pass" and rec.checks_sha == tip:
            checks_note = "\n✅ checks passed on this exact revision"
        elif rec.checks == "pass":
            checks_note = (
                "\n⚠️ checks passed earlier, but the branch moved since — "
                "consider run_checks again")
        else:
            checks_note = "\n⚠️ no checks recorded — consider run_checks first"

        return DeliveryPlan(
            thread_id=thread_id, rec=rec, repo=repo, worktree=worktree,
            branch=branch, base=base, tip=tip, base_tip=base_tip,
            checks_note=checks_note,
        ), None

    async def _deliver_worker(self, worker_id: str, method: str) -> dict[str, Any]:
        """Deliver now in balanced mode, or bind an approval to this revision."""
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        plan, problem = await self._delivery_preflight(worker_id, method)
        if plan is None:
            return err(problem or "delivery preflight failed")
        rec = plan.rec

        if self.settings.deploy_braveness == "balanced":
            result = await self._execute_delivery(
                rec.worker_id, method, expected_sha=plan.tip,
                expected_base_sha=plan.base_tip)
            self._action(f"deliver_worker({rec.worker_id}, {method})")
            return {"content": [{"type": "text", "text": result}]}

        # Conservative approval authorizes one observed revision, not a moving branch.
        self._pending_delivery[rec.worker_id] = {
            "method": method, "sha": plan.tip, "base_sha": plan.base_tip,
        }
        self.store.set_pending_delivery(self._pending_delivery)
        verb = "open a pull request for" if method == "pr" else "locally merge"
        await self._post(Outbound(
            thread_id=self._last_boss_thread, speaker=SYSTEM,
            text=(f"🚦 {rec.name} is ready to deliver. Approve to {verb} their work "
                  f"at {plan.tip[:8]} into '{plan.base}'?{plan.checks_note}\n"
                  f"    /approve {rec.worker_id}    ·    /reject {rec.worker_id}")))
        await self._refresh_dashboard()
        self._action(f"deliver_worker({rec.worker_id}, {method}) → 🚦")
        return {"content": [{"type": "text", "text":
                f"delivery of {rec.name} via {method} requested — the boss must "
                f"/approve {rec.worker_id} to authorize it (I can't land it myself)."}]}

    async def approve_delivery(self, worker_id: str) -> str:
        """The hard gate: called only by an allowlisted human's /approve, so an
        injected orchestrator can't self-authorize a push/merge."""
        wid = worker_id.strip().lower()
        pending = self._pending_delivery.pop(wid, None)
        if pending is None:
            return f"No pending delivery for '{wid}'."
        self.store.set_pending_delivery(self._pending_delivery)
        if isinstance(pending, str):
            return (
                f"Delivery request for '{wid}' predates revision-bound approvals. "
                "Review it and request delivery again.")
        return await self._execute_delivery(
            wid, str(pending.get("method", "")),
            expected_sha=str(pending.get("sha", "")) or None,
            expected_base_sha=str(pending.get("base_sha", "")) or None)

    async def reject_delivery(self, worker_id: str) -> str:
        wid = worker_id.strip().lower()
        method = self._pending_delivery.pop(wid, None)
        if method is None:
            return f"No pending delivery for '{wid}'."
        self.store.set_pending_delivery(self._pending_delivery)
        found = self._find_worker(wid)
        name = found[1].name if found else wid
        self._note(f"the boss rejected delivery of {name}")
        await self._refresh_dashboard()
        await self._wake_orchestrator()
        return f"Rejected {name}'s delivery; the orchestrator has been told."

    async def _execute_delivery(
        self, worker_id: str, method: str, expected_sha: str | None = None,
        expected_base_sha: str | None = None,
    ) -> str:
        plan, problem = await self._delivery_preflight(
            worker_id, method, expected_sha=expected_sha,
            expected_base_sha=expected_base_sha)
        if plan is None:
            return f"⚠️ delivery refused: {problem}"
        rec = plan.rec
        if method == "pr":
            landed, detail = await worktrees.open_pr(
                plan.repo, plan.branch, plan.base)
        else:
            landed, detail = await worktrees.merge_into_base(
                plan.repo, plan.branch, plan.base)
        if not landed:
            self._note(f"delivery of {rec.name} failed: {detail}")
            await self._wake_orchestrator()
            return f"⚠️ delivery of {rec.name} failed: {detail}"
        self.store.update(plan.thread_id, worker_status="delivered")
        await self._retire_worker_runtime(plan.thread_id)
        await self._post(Outbound(
            thread_id=plan.thread_id, speaker=SYSTEM, text=f"📦 {detail}."))
        self._note(f"{rec.name}'s work delivered — {detail}")
        await self._refresh_dashboard()
        await self._wake_orchestrator()
        return f"✅ {rec.name}'s work delivered — {detail}"

    # ---- direct sessions (orchestrator-less, via /new) -------------------

    async def new_direct(self, path_raw: str, name: str | None) -> tuple[str, str] | str:
        """Create a direct session thread. Returns (thread_id, name) or error str."""
        p = Path(path_raw).expanduser()
        if not p.is_absolute():
            p = self.settings.projects_root / path_raw
        p = p.resolve()
        if not p.is_dir():
            return f"❌ Not a directory: {p}"
        title = name or p.name
        assert self.transport is not None
        thread_id = await self.transport.create_thread(title)
        rec = ThreadRecord(
            role="direct", name=title, cwd=str(p),
            backend=self.settings.agent_backend)
        self.store.put(thread_id, rec)
        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return "❌ session failed to start"
        return thread_id, title

    async def interrupt(self, thread_id: str) -> bool:
        # /stop in a DM should stop the orchestrator — its session lives under the
        # main thread, not the DM's id.
        if self._is_orchestrator_thread(thread_id):
            thread_id = self.main_thread
        session = self.sessions.get(thread_id)
        if session is None or session.status not in ("busy", "waiting"):
            return False  # honest: there was nothing running to stop
        await session.interrupt()
        return True

    async def kill(self, thread_id: str) -> bool:
        rec = self.store.get(thread_id)
        if rec is not None and rec.role == "project_manager":
            result = await self._dismiss_project_manager(rec.manager_id)
            return not bool(result.get("is_error"))
        session = self.sessions.pop(thread_id, None)
        if session is not None:
            await session.stop()
        if rec is None:
            return session is not None
        if rec.role == "worker" and rec.repo and rec.cwd != rec.repo:
            await worktrees.terminate_worker_processes(
                Path(rec.cwd), worker_thread_id=thread_id)
            await worktrees.remove_worktree(
                Path(rec.repo), Path(rec.cwd), worker_thread_id=thread_id)
        if rec.role == "orchestrator":
            self.store.set_orchestrator_thread(None)
        self.store.delete(thread_id)
        self._session_locks.pop(thread_id, None)  # don't accumulate dead locks
        if rec.role == "worker":
            await self._refresh_dashboard()
        return True

    def listing(self) -> list[tuple[str, ThreadRecord, str]]:
        rows = []
        for tid, rec in self.store.all().items():
            live = self.sessions.get(tid)
            rows.append((tid, rec, live.status if live else "dormant"))
        return rows

    def rehydrate(self) -> None:
        """After a restart, re-surface every non-terminal worker.

        The supervision inbox and queued turns are in-memory, so a restart interrupts
        a working worker as surely as it can strand a finished or blocked one.
        Re-enqueue all of them for startup_recovery() to deliver to the orchestrator.
        """
        managers = [rec for rec in self.store.managers().values()
                    if rec.manager_status != "dismissed"]
        if managers:
            summaries = "; ".join(
                f"{m.manager_id}={m.last_summary[:120] or 'no prior summary'}"
                for m in managers)
            self._note(
                f"[after restart] project managers restored dormant: {summaries}. "
                "Their native sessions resume lazily; dormant is healthy.")
        legacy_pending: list[ThreadRecord] = []
        for rec in self.store.workers().values():
            if rec.worker_status not in ("working", "blocked", "done"):
                continue
            note = (
                f"[after restart] {rec.name} ({rec.worker_status}) needs recovery. "
                "Working turns are resumed automatically; inspect current workspace "
                "state and do not duplicate external side effects.")
            manager = self._find_manager(rec.supervisor_id) if rec.supervisor_id else None
            if manager:
                self._manager_note(manager[0], note)
            else:
                legacy_pending.append(rec)
        if legacy_pending:
            names = ", ".join(
                f"{worker.name} ({worker.worker_status})"
                for worker in legacy_pending)
            self._note(
                f"[after restart] non-terminal workers need recovery: {names}. "
                "Working workers are resumed automatically; inspect current workspace "
                "state and do not duplicate external side effects.")

    async def startup_recovery(self) -> None:
        """Resume interrupted workers, then wake the orchestrator with ground truth."""
        for thread_id, rec in self.store.workers().items():
            if rec.worker_status != "working":
                continue
            session = await self._ensure_session(thread_id, rec)
            if session is None:
                await self._route_worker_supervision(
                    rec, f"automatic restart recovery could not start {rec.name}; "
                    "inspect and re-brief them")
                continue
            await self._post(Outbound(
                thread_id=thread_id, speaker=SYSTEM,
                text="♻️ Resuming interrupted work after service restart."))
            await session.submit(
                "[AUTOMATIC RESTART RECOVERY]\n"
                "Your previous in-memory turn was interrupted by a service restart. "
                "Continue the original task from the persisted workspace. Before acting, "
                "inspect git status/log and existing artifacts; preserve completed work "
                "and do not blindly replay external side effects. Re-establish the latest "
                "safe checkpoint, finish or report the real blocker, and end with your "
                "normal STATUS line.\n\n"
                f"Persisted original brief:\n{rec.task}")
            log.info("automatically resumed worker %s after restart", rec.worker_id)
        pending = list(self.store.pending_boss_turns)
        if pending:
            await self._ensure_orchestrator(self.main_thread)
            rec = self.store.get(self.main_thread)
            session = await self._ensure_session(self.main_thread, rec) if rec else None
            if session is not None:
                for turn in pending:
                    self._last_boss_thread = turn["thread_id"]
                    self.store.set_last_boss_thread(turn["thread_id"])
                    text = (
                        "[AUTOMATIC BOSS TURN RECOVERY after a service/backend "
                        "failure; act on this request now. "
                        f"Fleet right now: {self._fleet_snapshot()}]\n"
                        + turn["text"])
                    await session.submit(
                        text, reply_to=turn["thread_id"],
                        delivery_ids=[turn["id"]])
                log.info("re-enqueued %d durable boss turn(s)", len(pending))
        for manager_thread in list(self._manager_inboxes):
            await self._wake_project_manager(manager_thread)
        await self._wake_orchestrator()

    async def factory_reset(self) -> str:
        """Wipe everything the bot knows: live sessions, all conversation memory
        (session histories), fleet records, worktrees, worker topics, and the
        dashboard. The next message starts from a true blank slate — a fresh
        implementation sees zero old context. Only reachable via a human's
        explicit `/reset confirm`."""
        for session in list(self.sessions.values()):
            try:
                await session.stop()
            except Exception:  # noqa: BLE001
                pass
        self.sessions.clear()
        self._inbox.clear()
        self._manager_inboxes.clear()
        self._manager_waking.clear()
        self._waking = False
        self._pending_delivery.clear()
        self._last_dashboard = ""
        self._last_boss_thread = self.main_thread

        delete_thread = getattr(self.transport, "delete_thread", None)
        for tid, rec in list(self.store.all().items()):
            if rec.role == "worker" and rec.repo and rec.cwd and rec.cwd != rec.repo:
                try:  # factory reset discards work in progress, dirty or not
                    await worktrees.force_remove_worktree(
                        Path(rec.repo), Path(rec.cwd), worker_thread_id=tid)
                except Exception:  # noqa: BLE001
                    pass
            # Keep the orchestrator's office(s) so you land on a fresh, empty
            # conversation — only worker/direct threads vanish, messages and all.
            if delete_thread is not None and not self._is_orchestrator_thread(tid):
                try:
                    await delete_thread(tid)
                except Exception:  # noqa: BLE001
                    pass
        delete_dashboard = getattr(self.transport, "delete_dashboard", None)
        if delete_dashboard is not None:
            try:
                await delete_dashboard()
            except Exception:  # noqa: BLE001
                pass
        # Wipe the surface's scrollback too, and tell live clients to clear their
        # message logs — otherwise the old conversation lingers on screen and replays
        # on the next reload. A true blank slate.
        reset = getattr(self.transport, "reset", None)
        if reset is not None:
            try:
                await reset()
            except Exception:  # noqa: BLE001
                pass

        self.store.wipe()
        # Read-only observer surfaces do not share the transport's in-memory reset;
        # replace their projection immediately so a clean slate cannot show stale
        # projects until the boss sends the next message.
        self._last_organization = None
        await self._refresh_organization()
        for sub in ("orchestrator-home", "managers", "worktrees"):
            shutil.rmtree(self.settings.state_dir / sub, ignore_errors=True)
        log.warning("factory reset executed — all state wiped")
        msg = ("🏭 Factory reset complete — memory, state, and worker threads wiped. "
               "Committed worker/* branches in your repos were left untouched.")
        # A surface may not be able to remove every old message from the screen (e.g.
        # Telegram can only delete messages it tracked). Let it own that caveat here so
        # the confirmation never over-claims a blank slate it didn't fully deliver.
        caveat = getattr(self.transport, "reset_caveat", "")
        return f"{msg}{caveat}" if caveat else msg

    async def shutdown(self) -> None:
        if self._retirement_tasks:
            await asyncio.gather(
                *tuple(self._retirement_tasks), return_exceptions=True)
        for session in list(self.sessions.values()):
            await session.stop()
        self.sessions.clear()
